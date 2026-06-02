#!/usr/bin/env python3
"""
🤖 Claude Bot Signal Tracker — 0xb55fa1296E6

Monitors the Claude Bot's on-chain trade activity and extracts:
  1. Directional bias (Up% vs Down%) — real-time sentiment signal
  2. Position accumulation alerts — when a market hits 5000+ shares
  3. Entry price zones — where the bot is buying

Stores signals to SQLite and writes a live JSON file for the dashboard.

Usage:
  python3 claude_signal.py
  python3 claude_signal.py --window 100 --min-shares 5000 --poll 30
"""

import os, sys, json, time, signal, logging, argparse, sqlite3
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict, Counter
from typing import Optional, Dict, List

# ─── Setup ───────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
for d in [LOG_DIR, DATA_DIR]:
    d.mkdir(parents=True, exist_ok=True)

CLAUDE_ADDR = "0xb55fa1296E6ec55D0cE53d93B9237389f11764d4"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "claude_signal.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("claude_signal")


# ─── Database ─────────────────────────────────────────────────────
DB_PATH = DATA_DIR / "claude_signal.db"
SIGNAL_FILE = DATA_DIR / "claude_signal.json"


def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            direction TEXT,
            strength REAL,
            up_pct REAL,
            down_pct REAL,
            trade_count INTEGER,
            details TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accumulations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            market_title TEXT,
            outcome TEXT,
            total_shares REAL,
            avg_price REAL,
            total_cost REAL,
            status TEXT DEFAULT 'active',
            resolved_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seen_at TEXT NOT NULL,
            tx_hash TEXT UNIQUE,
            condition_id TEXT,
            market_title TEXT,
            side TEXT,
            outcome TEXT,
            price REAL,
            size REAL,
            timestamp INTEGER
        )
    """)
    conn.commit()
    return conn


# ─── API Fetch ────────────────────────────────────────────────────
def fetch_activity(addr: str, limit: int = 100, sort_by: str = "TIMESTAMP",
                   sort_dir: str = "DESC", activity_type: str = "TRADE",
                   retries: int = 3) -> List[dict]:
    """Fetch recent activity from Polymarket Data API with retry."""
    import urllib.request, ssl, urllib.parse

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    params = urllib.parse.urlencode({
        "user": addr,
        "limit": str(limit),
        "sortBy": sort_by,
        "sortDirection": sort_dir,
        "type": activity_type,
    })
    url = f"https://data-api.polymarket.com/activity?{params}"

    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                data = json.loads(resp.read())
                if isinstance(data, list):
                    return data
                else:
                    logger.warning(f"API returned non-list: {str(data)[:200]}")
                    return []
        except Exception as e:
            if attempt < retries - 1:
                wait = 2 ** attempt
                logger.warning(f"API fetch error (attempt {attempt+1}/{retries}): {e}, retrying in {wait}s")
                time.sleep(wait)
            else:
                logger.error(f"API fetch failed after {retries} attempts: {e}")
                return []


# ─── Signal Analysis ──────────────────────────────────────────────
class ClaudeSignalTracker:
    def __init__(self, window: int = 100, min_shares: int = 5000):
        self.window = window
        self.min_shares = min_shares
        self.conn = init_db()
        self.seen_tx_hashes: set = set()

    def load_seen_hashes(self):
        """Load already-seen transaction hashes from DB."""
        rows = self.conn.execute(
            "SELECT tx_hash FROM raw_trades ORDER BY id DESC LIMIT 5000"
        ).fetchall()
        self.seen_tx_hashes = {r[0] for r in rows if r[0]}

    def analyze(self, trades: List[dict]) -> dict:
        """Analyze a batch of trades and return signal data."""

        if not trades:
            return {"signal": "NO_DATA", "error": "No trades fetched"}

        # Filter to BUY only (entries)
        buys = [t for t in trades if t.get("side") == "BUY"]

        # Store new trades
        new_count = 0
        for t in buys:
            tx_hash = t.get("transactionHash", "")
            if tx_hash and tx_hash not in self.seen_tx_hashes:
                try:
                    self.conn.execute(
                        """INSERT OR IGNORE INTO raw_trades 
                           (seen_at, tx_hash, condition_id, market_title, side, outcome, price, size, timestamp)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            datetime.now(timezone.utc).isoformat(),
                            tx_hash,
                            t.get("conditionId", ""),
                            t.get("title", ""),
                            t.get("side", ""),
                            t.get("outcome", ""),
                            float(t.get("price", 0)),
                            float(t.get("size", 0)),
                            int(t.get("timestamp", 0)),
                        ),
                    )
                    self.seen_tx_hashes.add(tx_hash)
                    new_count += 1
                except Exception:
                    pass
        if new_count:
            self.conn.commit()

        # ── 1. Directional Bias ──
        outcomes = [t.get("outcome", "?") for t in buys if t.get("outcome")]
        outcome_counts = Counter(outcomes)
        total = sum(outcome_counts.values())
        up_pct = outcome_counts.get("Up", 0) / total * 100 if total else 0
        down_pct = outcome_counts.get("Down", 0) / total * 100 if total else 0

        # Determine signal
        signal_type = "NEUTRAL"
        direction = None
        strength = 0.0

        if down_pct >= 80:
            signal_type = "STRONG_DOWN"
            direction = "Down"
            strength = down_pct / 100
        elif down_pct >= 65:
            signal_type = "DOWN_BIAS"
            direction = "Down"
            strength = down_pct / 100
        elif up_pct >= 80:
            signal_type = "STRONG_UP"
            direction = "Up"
            strength = up_pct / 100
        elif up_pct >= 65:
            signal_type = "UP_BIAS"
            direction = "Up"
            strength = up_pct / 100

        # ── 2. Position Accumulation ──
        accumulations = defaultdict(lambda: {
            "shares": 0, "cost": 0, "outcomes": [], "title": "", "trades": 0
        })
        for t in buys:
            cid = t.get("conditionId", "")
            price = float(t.get("price", 0))
            size = float(t.get("size", 0))
            accumulations[cid]["shares"] += size
            accumulations[cid]["cost"] += price * size
            accumulations[cid]["outcomes"].append(t.get("outcome", "?"))
            accumulations[cid]["title"] = t.get("title", "")[:80]
            accumulations[cid]["trades"] += 1

        # Big accumulations
        big_accumulations = []
        for cid, acc in accumulations.items():
            if acc["shares"] >= self.min_shares:
                avg_price = acc["cost"] / acc["shares"] if acc["shares"] > 0 else 0
                majority_outcome = Counter(acc["outcomes"]).most_common(1)[0][0]
                big_accumulations.append({
                    "condition_id": cid,
                    "title": acc["title"],
                    "shares": round(acc["shares"], 1),
                    "avg_price": round(avg_price, 4),
                    "cost": round(acc["cost"], 2),
                    "outcome": majority_outcome,
                    "trades": acc["trades"],
                })

        big_accumulations.sort(key=lambda x: -x["shares"])

        # ── 3. Price Zones ──
        prices = [float(t.get("price", 0)) for t in buys if float(t.get("price", 0)) < 0.80]
        price_zones = {}
        if prices:
            zones = [
                ("snipes", 0.01, 0.10),
                ("cheap", 0.10, 0.30),
                ("mid", 0.30, 0.50),
                ("high", 0.50, 0.70),
            ]
            for zone_name, lo, hi in zones:
                in_zone = [p for p in prices if lo <= p < hi]
                if in_zone:
                    price_zones[zone_name] = {
                        "count": len(in_zone),
                        "avg": round(sum(in_zone) / len(in_zone), 3),
                        "pct": round(len(in_zone) / len(prices) * 100, 1),
                    }

        # ── 4. Market Distribution ──
        markets = Counter(t.get("slug", "?") for t in buys)
        top_markets = [
            {"slug": slug, "count": count}
            for slug, count in markets.most_common(5)
        ]

        # ── Assemble Result ──
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "addr": CLAUDE_ADDR,
            "signal": signal_type,
            "direction": direction,
            "strength": round(strength, 3),
            "bias": {
                "up_pct": round(up_pct, 1),
                "down_pct": round(down_pct, 1),
                "total_trades": total,
            },
            "accumulations": big_accumulations[:10],
            "price_zones": price_zones,
            "top_markets": top_markets,
            "meta": {
                "trades_fetched": len(trades),
                "new_trades": new_count,
                "window": self.window,
            },
        }

        return result

    def save_signal(self, result: dict):
        """Save signal to DB and JSON file."""
        # DB
        self.conn.execute(
            """INSERT INTO signals (timestamp, signal_type, direction, strength, up_pct, down_pct, trade_count, details)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result["timestamp"],
                result["signal"],
                result["direction"],
                result["strength"],
                result["bias"]["up_pct"],
                result["bias"]["down_pct"],
                result["bias"]["total_trades"],
                json.dumps(result, default=str),
            ),
        )
        self.conn.commit()

        # Save accumulations
        for acc in result["accumulations"]:
            self.conn.execute(
                """INSERT OR IGNORE INTO accumulations 
                   (detected_at, condition_id, market_title, outcome, total_shares, avg_price, total_cost)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    result["timestamp"],
                    acc["condition_id"],
                    acc["title"],
                    acc["outcome"],
                    acc["shares"],
                    acc["avg_price"],
                    acc["cost"],
                ),
            )
        self.conn.commit()

        # JSON file for dashboard
        with open(SIGNAL_FILE, "w") as f:
            json.dump(result, f, indent=2, default=str)

    def run_once(self):
        """One polling cycle."""
        trades = fetch_activity(CLAUDE_ADDR, limit=self.window, sort_by="TIMESTAMP",
                                sort_dir="DESC", activity_type="TRADE")
        
        if not trades:
            logger.warning("No trades fetched, skipping cycle")
            return None

        result = self.analyze(trades)
        self.save_signal(result)

        # Log summary
        sig = result["signal"]
        bias = result["bias"]
        acc_count = len(result["accumulations"])
        logger.info(
            f"Signal: {sig} | Up {bias['up_pct']}% / Down {bias['down_pct']}% "
            f"({bias['total_trades']} trades) | {acc_count} accumulations | "
            f"{result['meta']['new_trades']} new"
        )

        return result


# ─── Main Loop ────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Claude Bot Signal Tracker")
    parser.add_argument("--window", type=int, default=100, help="Trade window size")
    parser.add_argument("--min-shares", type=int, default=5000, help="Min shares for accumulation alert")
    parser.add_argument("--poll", type=int, default=30, help="Poll interval seconds")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()

    tracker = ClaudeSignalTracker(window=args.window, min_shares=args.min_shares)
    tracker.load_seen_hashes()

    logger.info(f"🚀 Claude Signal Tracker started | window={args.window} "
                f"min_shares={args.min_shares} poll={args.poll}s")
    logger.info(f"   Address: {CLAUDE_ADDR}")

    running = True

    def handle_signal(sig, frame):
        nonlocal running
        logger.info("Shutting down...")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    if args.once:
        result = tracker.run_once()
        if result:
            print(json.dumps(result, indent=2, default=str))
        return

    while running:
        try:
            tracker.run_once()
        except Exception as e:
            logger.error(f"Cycle error: {e}", exc_info=True)

        if running:
            time.sleep(args.poll)

    tracker.conn.close()
    logger.info("Stopped.")


if __name__ == "__main__":
    main()
