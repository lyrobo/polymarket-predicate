"""
🐦 Annica Sim Copy Trader — $700 Virtual Balance (Elon Tweet Strategy)
🐦 Annica Sim Copy Trader — $700 Virtual Balance (Elon Tweet Strategy)
Tracks Annica's activity and simulates copying ALL trades with virtual balance.
Strategy: Copy ALL trades — YES and NO — at any price.
When he SELLs → close our matching position.
When he REDEEMs → resolve at $1.00/share.

Usage:
  python3 annica_sim.py
  python3 annica_sim.py --ratio 0.05 --balance 700
"""

import os, sys, json, time, signal, logging, argparse, sqlite3, subprocess
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict
from typing import Optional, List, Dict, Set

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
for d in [LOG_DIR, DATA_DIR]:
    d.mkdir(parents=True, exist_ok=True)

ANNICA_WALLET = "0x689ae12e11aa489adb3605afd8f39040ff52779e"
POLL_INTERVAL = 10  # Annica trades less frequently, longer poll is fine
CURL_CMD = ["curl", "-s", "--connect-timeout", "3", "--max-time", "10"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "annica_sim.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("annica_sim")

# ─── Database ───────────────────────────────────────────────────
DB_PATH = DATA_DIR / "annica_sim.db"


def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sim_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            copied_at TEXT NOT NULL,
            annica_activity_id TEXT,
            market_slug TEXT NOT NULL,
            market_title TEXT,
            annica_side TEXT NOT NULL,
            annica_price REAL NOT NULL,
            annica_size_usdc REAL NOT NULL,
            our_side TEXT NOT NULL,
            our_price REAL NOT NULL,
            our_shares INTEGER NOT NULL,
            our_cost REAL NOT NULL,
            outcome_index INTEGER DEFAULT 0,
            token_id TEXT,
            status TEXT DEFAULT 'open',
            resolved_pnl REAL DEFAULT 0,
            resolved_at TEXT,
            notes TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sim_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn


# ─── Activity Fetch ─────────────────────────────────────────────
def curl(url: str) -> Optional[dict]:
    try:
        # Use system curl to avoid certifi issues
        r = subprocess.run(
            ["/usr/bin/env", "-u", "SSL_CERT_FILE", "-u", "REQUESTS_CA_BUNDLE",
             "/usr/bin/curl", "-s", "--connect-timeout", "3", "--max-time", "10", url],
            capture_output=True, text=True, timeout=12,
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except:
        pass
    return None


def fetch_annica_activity(limit: int = 50) -> List[dict]:
    all_acts = []
    for offset in [0, 50, 100, 150]:
        data = curl(
            f"https://data-api.polymarket.com/activity?user={ANNICA_WALLET}&limit={limit}&offset={offset}"
        )
        if data and isinstance(data, list) and len(data) > 0:
            all_acts.extend(data)
        else:
            break
    return all_acts


# ─── Sim Trader ─────────────────────────────────────────────────
class SimTrader:
    def __init__(self, conn: sqlite3.Connection, balance: float = 700.0, ratio: float = 0.05,
                 max_price: float = 0.95):
        self.conn = conn
        self.initial_balance = balance
        self.balance = balance
        self.ratio = ratio
        self.max_price = max_price  # only copy YES buys below this price
        self.known_ids: Set[str] = set()
        self._load_known()

    def _load_known(self):
        rows = self.conn.execute("SELECT annica_activity_id FROM sim_trades").fetchall()
        self.known_ids = {r[0] for r in rows if r[0]}
        row = self.conn.execute("SELECT value FROM sim_state WHERE key='known_ids'").fetchone()
        if row:
            try:
                extra = json.loads(row[0])
                self.known_ids.update(extra)
            except:
                pass

    def _save_known(self):
        self.conn.execute(
            "INSERT OR REPLACE INTO sim_state (key, value) VALUES ('known_ids', ?)",
            (json.dumps(list(self.known_ids)[-5000:]),),
        )
        self.conn.commit()

    def _event_id(self, act: dict) -> str:
        tx = act.get("transactionHash", "")
        oid = act.get("orderID", act.get("id", ""))
        return tx or oid or f"{act.get('timestamp','')}_{act.get('type','')}_{act.get('size','')}"

    def _available_balance(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(our_cost), 0) FROM sim_trades WHERE status='open'"
        ).fetchone()
        total_invested = row[0]
        return max(0, self.initial_balance - total_invested)

    def sim_buy(self, act: dict):
        """Simulate copying Annica's buy — YES or NO."""
        eid = self._event_id(act)
        if eid in self.known_ids:
            return

        annica_side = act.get("side", "")
        annica_price = float(act.get("price", 0))
        annica_size = float(act.get("usdcSize", 0))
        outcome = act.get("outcome", "")
        slug = act.get("slug", "")
        title = act.get("title", "")[:80]
        oi = int(act.get("outcomeIndex", 0))
        ts = act.get("timestamp", "")

        # Copy ALL trades — YES and NO
        if isinstance(ts, (int, float)):
            ts = datetime.utcfromtimestamp(ts).isoformat()

        # Calculate our simulated trade
        our_cost = annica_size * self.ratio
        if our_cost < 0.10:  # skip sub-10-cent trades
            self.known_ids.add(eid)
            return

        our_shares = max(1, int(our_cost / annica_price)) if annica_price > 0 else 1
        our_cost = our_shares * annica_price

        # Balance check — allow using up to 50% for a single trade
        avail = self._available_balance()
        if our_cost > avail * 0.5:
            logger.debug(f"  ⏭ Balance: avail=${avail:.2f}, need=${our_cost:.2f}")
            self.known_ids.add(eid)
            return

        self.conn.execute(
            """INSERT INTO sim_trades 
               (copied_at, annica_activity_id, market_slug, market_title,
                annica_side, annica_price, annica_size_usdc,
                our_side, our_price, our_shares, our_cost,
                outcome_index, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
            (ts, eid, slug, title,
             annica_side, annica_price, annica_size,
             annica_side, annica_price, our_shares, our_cost,
             oi),
        )
        self.conn.commit()
        self.known_ids.add(eid)
        self._save_known()

        logger.info(
            f"🎫 COPY {outcome}: {our_shares:>6}sh @ ${annica_price:.4f} "
            f"cost=${our_cost:.2f} | {title[:50]}"
        )

    def handle_sell(self, act: dict):
        """When Annica sells, close our matching position."""
        slug = act.get("slug", "")
        annica_price = float(act.get("price", 0))
        title = act.get("title", "")[:80]
        ts = act.get("timestamp", "")
        if isinstance(ts, (int, float)):
            ts = datetime.utcfromtimestamp(ts).isoformat()

        rows = self.conn.execute(
            "SELECT id, our_shares, our_cost FROM sim_trades WHERE market_slug=? AND status='open'",
            (slug,),
        ).fetchall()

        if not rows:
            return

        for row in rows:
            tid, shares, cost = row
            payout = shares * annica_price
            pnl = payout - cost
            self.conn.execute(
                "UPDATE sim_trades SET status='resolved', resolved_pnl=?, resolved_at=?, notes=? WHERE id=?",
                (round(pnl, 4), ts, f"Matched Annica sell @ ${annica_price:.4f}", tid),
            )
            self.conn.commit()
            emoji = "🟢" if pnl > 0 else "🔴"
            logger.info(
                f"{emoji} CLOSED #{tid}: {shares}sh @ ${annica_price:.4f} | "
                f"cost=${cost:.2f} | PnL=${pnl:+.2f} | {title[:35]}"
            )

    def handle_redeem(self, act: dict):
        """When Annica redeems, resolve at $1.00 per share (YES or NO winners)."""
        slug = act.get("slug", "")
        title = act.get("title", "")[:80]
        ts = act.get("timestamp", "")
        if isinstance(ts, (int, float)):
            ts = datetime.utcfromtimestamp(ts).isoformat()

        rows = self.conn.execute(
            "SELECT id, our_shares, our_cost FROM sim_trades WHERE market_slug=? AND status='open'",
            (slug,),
        ).fetchall()

        if not rows:
            logger.debug(f"  No open positions for redeem: {slug}")
            return

        for row in rows:
            tid, shares, cost = row
            payout = shares  # $1.00 per share for winning YES
            pnl = payout - cost
            self.conn.execute(
                "UPDATE sim_trades SET status='resolved', resolved_pnl=?, resolved_at=?, notes=? WHERE id=?",
                (round(pnl, 4), ts, f"Redeemed YES: payout ${payout:.2f} @ $1.00/sh", tid),
            )
            self.conn.commit()
            emoji = "🟢" if pnl > 0 else "🔴"
            logger.info(
                f"{emoji} REDEEMED #{tid}: {shares}sh → ${payout:.2f} | "
                f"cost=${cost:.2f} | PnL=${pnl:+.2f} | {title[:35]}"
            )

    def process_events(self, activities: List[dict]):
        """Process new events from Annica.
        
        BUY  YES/NO → sim_buy (copy all)
        SELL     → handle_sell (close matching position)
        REDEEM   → handle_redeem (resolve at $1.00/share)
        """
        buys = []
        sells = []
        redeems = []

        for act in activities:
            eid = self._event_id(act)
            if eid in self.known_ids:
                continue
            t = act.get("type", "")
            if t == "TRADE":
                side = act.get("side", "")
                if side == "SELL":
                    sells.append(act)
                else:
                    buys.append(act)
            elif t == "REDEEM":
                redeems.append(act)

        # Process: buys first (open), then sells/redeems (close)
        for b in buys:
            self.sim_buy(b)

        for s in sells:
            self.handle_sell(s)

        for r in redeems:
            self.handle_redeem(r)

    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) FROM sim_trades").fetchone()[0]
        open_count = self.conn.execute(
            "SELECT COUNT(*) FROM sim_trades WHERE status='open'"
        ).fetchone()[0]
        resolved = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(resolved_pnl),0) FROM sim_trades WHERE status='resolved'"
        ).fetchone()
        cost_open = self.conn.execute(
            "SELECT COALESCE(SUM(our_cost),0) FROM sim_trades WHERE status='open'"
        ).fetchone()[0]

        # Calculate unrealized PnL for open positions (assume current YES price ≈ buy price)
        unrealized = self.conn.execute(
            "SELECT COALESCE(SUM(our_shares * our_price - our_cost), 0) FROM sim_trades WHERE status='open'"
        ).fetchone()[0]

        return {
            "total_trades": total,
            "open": open_count,
            "resolved": resolved[0],
            "realized_pnl": round(resolved[1], 2),
            "open_cost": round(cost_open, 2),
            "unrealized": round(unrealized, 2),
            "avail_balance": round(self._available_balance(), 2),
        }


# ─── Main ───────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ratio", type=float, default=0.05, help="Copy ratio vs Annica size (default: 5%%)")
    p.add_argument("--balance", type=float, default=700.0, help="Virtual balance (default: $700)")
    p.add_argument("--max-price", type=float, default=0.99,
                   help="Max price to copy (default: $0.99, effectively all)")
    args = p.parse_args()

    load_dotenv(dotenv_path=BASE_DIR / ".env")

    conn = init_db()
    trader = SimTrader(conn, balance=args.balance, ratio=args.ratio, max_price=args.max_price)

    logger.info("=" * 60)
    logger.info(f"🐦 Annica SIM Copy Trader — Virtual ${args.balance:,.0f}")
    logger.info(f"   Target: @Annica ({ANNICA_WALLET[:10]}...)")
    logger.info(f"   Strategy: Copy ALL trades (YES + NO)")
    logger.info(f"   Ratio: {args.ratio:.0%} | Poll: {POLL_INTERVAL}s")
    logger.info(f"   DB: {DB_PATH}")
    logger.info("=" * 60)

    # Initialize: seed known IDs from existing activity
    logger.info("Seeding known activity...")
    existing = fetch_annica_activity(50)
    for act in existing:
        trader.known_ids.add(trader._event_id(act))
    trader._save_known()
    logger.info(f"  Seeded {len(existing)} existing events (will not simulate)")

    cycles = 0
    running = True
    signal.signal(signal.SIGINT, lambda *a: setattr(sys.modules[__name__], "running", False))

    while running:
        try:
            cycles += 1
            activities = fetch_annica_activity(50)
            trader.process_events(activities)

            if cycles % 6 == 0:  # every ~60s
                s = trader.stats()
                logger.info(
                    f"📊 C#{cycles} | trades={s['total_trades']} "
                    f"open={s['open']} resolved={s['resolved']} "
                    f"realizedPnL=${s['realized_pnl']:+.2f} "
                    f"openCost=${s['open_cost']:.2f} "
                    f"avail=${s['avail_balance']:.2f}"
                )

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Cycle error: {e}")
            time.sleep(10)

    logger.info("🏁 Annica sim bot stopped")


if __name__ == "__main__":
    main()
