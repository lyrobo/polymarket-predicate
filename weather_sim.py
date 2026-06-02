"""
🌤 WeatherHK Sim Copy Trader — $700 Virtual Balance

Tracks WeatherHK's activity and simulates copying with $700 virtual balance.
Records every trade with PnL tracking. No real orders — simulation only.

Usage:
  python3 weather_sim.py
  python3 weather_sim.py --ratio 0.02 --balance 700
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

WHK_WALLET = "0x488c725253fc21c7a9ca812030dc2f6343f98c1c"
POLL_INTERVAL = 5
CURL_CMD = ["curl", "-s", "--connect-timeout", "2", "--max-time", "5"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "weather_sim.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("weather_sim")

# ─── Database ───────────────────────────────────────────────────
DB_PATH = DATA_DIR / "weather_sim.db"


def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sim_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            copied_at TEXT NOT NULL,
            whk_activity_id TEXT,
            market_slug TEXT NOT NULL,
            market_title TEXT,
            whk_side TEXT NOT NULL,
            whk_price REAL NOT NULL,
            whk_size_usdc REAL NOT NULL,
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
        r = subprocess.run(CURL_CMD + [url], capture_output=True, text=True, timeout=6)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except:
        pass
    return None


def fetch_weatherhk_activity(limit: int = 100) -> List[dict]:
    all_acts = []
    for offset in [0, 50, 100]:
        data = curl(
            f"https://data-api.polymarket.com/activity?user={WHK_WALLET}&limit={limit}&offset={offset}"
        )
        if data and isinstance(data, list):
            all_acts.extend(data)
    return all_acts


# ─── Sim Trader ─────────────────────────────────────────────────
class SimTrader:
    def __init__(self, conn: sqlite3.Connection, balance: float = 700.0, ratio: float = 0.02):
        self.conn = conn
        self.initial_balance = balance
        self.balance = balance
        self.ratio = ratio
        self.known_ids: Set[str] = set()
        self._load_known()

    def _load_known(self):
        rows = self.conn.execute("SELECT whk_activity_id FROM sim_trades").fetchall()
        self.known_ids = {r[0] for r in rows if r[0]}
        # Also load from state
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
        """Calculate available balance (initial - total cost of open positions)."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(our_cost), 0) FROM sim_trades WHERE status='open'"
        ).fetchone()
        total_invested = row[0]
        return max(0, self.initial_balance - total_invested)

    def sim_trade(self, act: dict):
        """Simulate copying one trade."""
        eid = self._event_id(act)
        if eid in self.known_ids:
            return

        whk_side = act.get("side", "")
        whk_price = float(act.get("price", 0))
        whk_size = float(act.get("usdcSize", 0))
        slug = act.get("slug", "")
        title = act.get("title", "")[:80]
        oi = int(act.get("outcomeIndex", 0))
        ts = act.get("timestamp", "")
        if isinstance(ts, (int, float)):
            ts = datetime.utcfromtimestamp(ts).isoformat()

        # Calculate our simulated trade
        our_cost = whk_size * self.ratio
        if our_cost < 0.5:  # skip micro trades
            self.known_ids.add(eid)
            return

        our_shares = max(1, int(our_cost / whk_price)) if whk_price > 0 else 1
        our_cost = our_shares * whk_price  # real cost based on shares

        # Balance check
        avail = self._available_balance()
        if our_cost > avail * 0.5:  # max 50% of available
            logger.debug(f"  ⏭ Balance: avail=${avail:.2f}, need=${our_cost:.2f}")
            self.known_ids.add(eid)
            return

        # Record the trade
        self.conn.execute(
            """INSERT INTO sim_trades 
               (copied_at, whk_activity_id, market_slug, market_title,
                whk_side, whk_price, whk_size_usdc,
                our_side, our_price, our_shares, our_cost,
                outcome_index, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
            (ts, eid, slug, title,
             whk_side, whk_price, whk_size,
             whk_side, whk_price, our_shares, our_cost,
             oi),
        )
        self.conn.commit()
        self.known_ids.add(eid)
        self._save_known()

        logger.info(
            f"📝 SIM {whk_side:4} {our_shares:5d}sh @ ${whk_price:.4f} "
            f"cost=${our_cost:.2f} | {title[:55]}"
        )

    def handle_redeem(self, act: dict):
        """When WeatherHK redeems a market, resolve our simulated positions."""
        slug = act.get("slug", "")
        title = act.get("title", "")[:80]
        ts = act.get("timestamp", "")
        if isinstance(ts, (int, float)):
            ts = datetime.utcfromtimestamp(ts).isoformat()

        # Find open positions in this market
        rows = self.conn.execute(
            "SELECT id, our_shares, our_cost, our_side FROM sim_trades WHERE market_slug=? AND status='open'",
            (slug,),
        ).fetchall()

        if not rows:
            logger.debug(f"  No open positions for redeem: {slug}")
            return

        for row in rows:
            tid, shares, cost, side = row
            # When WeatherHK redeems NO, it resolves to $1.00
            # Our simulated profit = shares * $1.00 - cost
            payout = shares  # $1.00 per share
            pnl = payout - cost
            self.conn.execute(
                "UPDATE sim_trades SET status='resolved', resolved_pnl=?, resolved_at=?, notes=? WHERE id=?",
                (round(pnl, 4), ts, f"Resolved via WHK redeem: payout ${payout:.2f}", tid),
            )
            self.conn.commit()
            emoji = "🟢" if pnl > 0 else "🔴"
            logger.info(
                f"{emoji} RESOLVED #{tid}: {shares}sh → payout ${payout:.2f} | "
                f"cost=${cost:.2f} | PnL=${pnl:+.2f} | {title[:40]}"
            )

    def handle_sell(self, act: dict):
        """When WeatherHK sells, close our matching position regardless of price.
        
        His SELL is ALWAYS an exit (profit-taking or cut-loss). 
        We close our matching BUY position at the same price.
        """
        slug = act.get("slug", "")
        whk_price = float(act.get("price", 0))
        title = act.get("title", "")[:80]
        ts = act.get("timestamp", "")
        if isinstance(ts, (int, float)):
            ts = datetime.utcfromtimestamp(ts).isoformat()

        rows = self.conn.execute(
            "SELECT id, our_shares, our_cost, our_side FROM sim_trades WHERE market_slug=? AND status='open'",
            (slug,),
        ).fetchall()

        if not rows:
            return

        for row in rows:
            tid, shares, cost, side = row
            payout = shares * whk_price
            pnl = payout - cost
            self.conn.execute(
                "UPDATE sim_trades SET status='resolved', resolved_pnl=?, resolved_at=?, notes=? WHERE id=?",
                (round(pnl, 4), ts, f"Matched WHK sell @ ${whk_price:.4f}", tid),
            )
            self.conn.commit()
            emoji = "🟢" if pnl > 0 else "🔴"
            logger.info(
                f"{emoji} CLOSED #{tid}: {shares}sh @ ${whk_price:.4f} | "
                f"cost=${cost:.2f} | PnL=${pnl:+.2f}"
            )

    def process_events(self, activities: List[dict]):
        """Process new events from WeatherHK.

        STRATEGY: Only copy BUY (his opening positions).
        SELL events are his profit-taking exits — we handle them
        by closing our matching positions, NOT by opening new shorts.
        """
        trades = []
        redeems = []
        sells = []

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
                    trades.append(act)
            elif t == "REDEEM":
                redeems.append(act)

        # Process in order: new BUY trades first, then handle sells/redeems
        for t in trades:
            self.sim_trade(t)

        # SELL: only handle as position-closing (never open new shorts)
        for s in sells:
            price = float(s.get("price", 0))
            if price > 0.85:
                self.handle_sell(s)  # close matching position
            else:
                # Low-price SELL = his profit-taking, NOT a new position.
                # We close our matching BUY position if we have one.
                self.handle_sell(s)

        for r in redeems:
            self.handle_redeem(r)

    def stats(self) -> dict:
        """Get current stats."""
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

        return {
            "total_trades": total,
            "open": open_count,
            "resolved": resolved[0],
            "realized_pnl": round(resolved[1], 2),
            "open_cost": round(cost_open, 2),
            "avail_balance": round(self._available_balance(), 2),
        }


# ─── Main ───────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ratio", type=float, default=0.02, help="Copy ratio (default: 2%%)")
    p.add_argument("--balance", type=float, default=700.0, help="Virtual balance (default: $700)")
    args = p.parse_args()

    load_dotenv(dotenv_path=BASE_DIR / ".env")

    conn = init_db()
    trader = SimTrader(conn, balance=args.balance, ratio=args.ratio)

    logger.info("=" * 60)
    logger.info(f"🌤 WeatherHK SIM Copy Trader — Virtual ${args.balance:,.0f}")
    logger.info(f"   Target: @weatherhk ({WHK_WALLET[:10]}...)")
    logger.info(f"   Ratio: {args.ratio:.0%} | Poll: {POLL_INTERVAL}s")
    logger.info(f"   DB: {DB_PATH}")
    logger.info("=" * 60)

    # Initialize: seed known IDs from existing activity
    logger.info("Seeding known activity...")
    existing = fetch_weatherhk_activity(50)
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
            activities = fetch_weatherhk_activity(100)
            trader.process_events(activities)

            if cycles % 12 == 0:  # every ~60s
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

    logger.info("🏁 Sim bot stopped")


if __name__ == "__main__":
    main()
