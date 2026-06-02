"""
🤖 Dolita Sim Copy Trader — $700 Virtual Balance (BTC 5-min Directional)

Tracks dolita's BUY+SELL activity on BTC Up/Down markets and simulates copying.
Strategy: Copy BUYs proportionally, resolve at SELL price or REDEEM ($1.00).

Usage:
  python3 dolita_sim.py
  python3 dolita_sim.py --ratio 0.03 --balance 700
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

DOLITA_WALLET = "0x8c901f67b036b5eebab4e1f2f904b8676743a904"
POLL_INTERVAL = 5  # seconds — dolita trades ~253/day, need fast polling
CURL_CMD = ["curl", "-s", "--connect-timeout", "2", "--max-time", "5"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "dolita_sim.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("dolita_sim")

# ─── Database ───────────────────────────────────────────────────
DB_PATH = DATA_DIR / "dolita_sim.db"


def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sim_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            copied_at TEXT NOT NULL,
            dolita_activity_id TEXT,
            market_slug TEXT NOT NULL,
            market_title TEXT,
            dolita_side TEXT NOT NULL,
            dolita_price REAL NOT NULL,
            dolita_size_usdc REAL NOT NULL,
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


def fetch_dolita_activity(limit: int = 100) -> List[dict]:
    """Fetch recent activity from dolita."""
    all_acts = []
    for offset in [0, 50, 100]:
        data = curl(
            f"https://data-api.polymarket.com/activity?user={DOLITA_WALLET}&limit={limit}&offset={offset}"
        )
        if data and isinstance(data, list) and len(data) > 0:
            all_acts.extend(data)
        else:
            break
    return all_acts


def fetch_dolita_positions() -> List[dict]:
    """Fetch dolita's current positions (for detecting settlements)."""
    data = curl(
        f"https://data-api.polymarket.com/positions?user={DOLITA_WALLET}&limit=500"
    )
    return data if isinstance(data, list) else []


# ─── Sim Trader ─────────────────────────────────────────────────
class SimTrader:
    def __init__(self, conn: sqlite3.Connection, balance: float = 700.0, ratio: float = 0.03,
                 price_min: float = 0.0, price_max: float = 1.0,
                 size_min: int = 0, size_max: int = 999999):
        self.conn = conn
        self.initial_balance = balance
        self.balance = balance
        self.ratio = ratio
        self.price_min = price_min
        self.price_max = price_max
        self.size_min = size_min
        self.size_max = size_max
        self.known_ids: Set[str] = set()
        self._load_known()

    def _load_known(self):
        rows = self.conn.execute("SELECT dolita_activity_id FROM sim_trades").fetchall()
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
            (json.dumps(list(self.known_ids)[-10000:]),),
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
        """Simulate copying dolita's BUY."""
        eid = self._event_id(act)
        if eid in self.known_ids:
            return

        dolita_side = act.get("side", "")
        if dolita_side != "BUY":
            self.known_ids.add(eid)
            return

        dolita_price = float(act.get("price", 0))
        dolita_size = float(act.get("usdcSize", 0))
        dolita_shares = float(act.get("size", 0))
        slug = act.get("slug", "")
        title = act.get("title", "")[:80]
        oi = int(act.get("outcomeIndex", 0))
        ts = act.get("timestamp", "")
        if isinstance(ts, (int, float)):
            ts = datetime.utcfromtimestamp(ts).isoformat()

        # ── Price/size filters ──
        if dolita_price < self.price_min or dolita_price > self.price_max:
            self.known_ids.add(eid)
            return
        if dolita_shares < self.size_min or dolita_shares > self.size_max:
            self.known_ids.add(eid)
            return

        # Calculate our simulated trade
        our_cost = dolita_size * self.ratio
        if our_cost < 0.10:  # skip micro trades
            self.known_ids.add(eid)
            return

        our_shares = max(1, int(our_cost / dolita_price)) if dolita_price > 0 else 1
        our_cost = our_shares * dolita_price  # actual cost based on shares

        # Balance check — allow up to 25% of available per position
        avail = self._available_balance()
        if our_cost > avail * 0.25:
            logger.debug(f"  ⏭ Balance: avail=${avail:.2f}, need=${our_cost:.2f}")
            self.known_ids.add(eid)
            return

        self.conn.execute(
            """INSERT INTO sim_trades 
               (copied_at, dolita_activity_id, market_slug, market_title,
                dolita_side, dolita_price, dolita_size_usdc,
                our_side, our_price, our_shares, our_cost,
                outcome_index, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
            (ts, eid, slug, title,
             dolita_side, dolita_price, dolita_size,
             dolita_side, dolita_price, our_shares, our_cost,
             oi),
        )
        self.conn.commit()
        self.known_ids.add(eid)
        self._save_known()

        logger.info(
            f"📝 COPY {dolita_side:3} {our_shares:>5}sh @ ${dolita_price:.4f} "
            f"cost=${our_cost:.2f} | {title[:55]}"
        )

    def handle_redeem(self, act: dict):
        """When dolita redeems, resolve our position at $1.00/share."""
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
            return

        for row in rows:
            tid, shares, cost = row
            payout = shares  # $1.00 per share for winner
            pnl = payout - cost
            self.conn.execute(
                "UPDATE sim_trades SET status='resolved', resolved_pnl=?, resolved_at=?, notes=? WHERE id=?",
                (round(pnl, 4), ts, f"Redeemed: payout ${payout:.2f} @ $1.00/sh", tid),
            )
            self.conn.commit()
            emoji = "🟢" if pnl > 0 else "🔴"
            logger.info(
                f"{emoji} REDEEMED #{tid}: {shares}sh → ${payout:.2f} | "
                f"cost=${cost:.2f} | PnL=${pnl:+.2f} | {title[:35]}"
            )

    def handle_sell(self, act: dict):
        """When dolita SELLs, resolve our position at the sell price."""
        slug = act.get("slug", "")
        title = act.get("title", "")[:80]
        sell_price = float(act.get("price", 0))
        sell_size = float(act.get("size", 0))
        outcome = act.get("outcome", "")
        ts = act.get("timestamp", "")
        if isinstance(ts, (int, float)):
            ts = datetime.utcfromtimestamp(ts).isoformat()

        if sell_price <= 0 or not slug:
            return

        # Find all open positions for this market
        rows = self.conn.execute(
            "SELECT id, our_shares, our_cost, our_price FROM sim_trades WHERE market_slug=? AND status='open'",
            (slug,),
        ).fetchall()

        if not rows:
            return

        for row in rows:
            tid, shares, cost, our_price = row
            # PnL = shares * (sell_price - buy_price)
            pnl = shares * (sell_price - our_price)
            self.conn.execute(
                "UPDATE sim_trades SET status='resolved', resolved_pnl=?, resolved_at=?, notes=? WHERE id=?",
                (round(pnl, 4), ts,
                 f"SELL: dolita sold {sell_size:.0f}sh @ ${sell_price:.4f} ({outcome})", tid),
            )
            self.conn.commit()
            emoji = "🟢" if pnl > 0 else "🔴"
            logger.info(
                f"{emoji} SOLD #{tid}: {shares}sh bought@${our_price:.4f} sold@${sell_price:.4f} | "
                f"cost=${cost:.2f} | PnL=${pnl:+.2f} | {title[:35]}"
            )

    def check_expired_positions(self, dolita_positions: List[dict]):
        """
        Compare our open positions against dolita's current positions.
        Only expire positions that are > 30 minutes old and no longer held by dolita.
        Newer positions may have SELL events we haven't caught yet.
        """
        # Build set of slugs dolita currently has open positions in
        dolita_active_slugs = set()
        for p in dolita_positions:
            slug = p.get("slug", p.get("conditionId", ""))
            if slug:
                dolita_active_slugs.add(slug)

        # Find our open positions that dolita no longer holds
        our_open = self.conn.execute(
            "SELECT id, market_slug, our_shares, our_cost, market_title, copied_at "
            "FROM sim_trades WHERE status='open'"
        ).fetchall()

        now = datetime.utcnow()
        now_iso = now.isoformat()
        grace_minutes = 30  # don't expire positions newer than this

        for row in our_open:
            tid, slug, shares, cost, title, copied_at = row
            if slug and slug not in dolita_active_slugs:
                # Check if position is old enough to expire
                try:
                    copied_dt = datetime.fromisoformat(copied_at)
                    age_minutes = (now - copied_dt).total_seconds() / 60
                except:
                    age_minutes = 999  # can't parse → assume old

                if age_minutes < grace_minutes:
                    continue  # too new, give SELL event time to arrive

                # Position no longer held by dolita AND old enough — expired worthless
                pnl = -cost  # lost entire investment
                self.conn.execute(
                    "UPDATE sim_trades SET status='resolved', resolved_pnl=?, resolved_at=?, notes=? WHERE id=?",
                    (round(pnl, 4), now_iso, "EXPIRED: dolita no longer holds", tid),
                )
                self.conn.commit()
                logger.info(
                    f"💀 EXPIRED #{tid}: {shares}sh → $0.00 | "
                    f"cost=${cost:.2f} | PnL=${pnl:+.2f} | {title[:35] if title else slug[:35]}"
                )

    def process_events(self, activities: List[dict], dolita_positions: List[dict]):
        """Process new events from dolita.
        
        BUY  → sim_buy (copy proportionally)
        SELL → handle_sell (resolve at sell price)
        REDEEM → handle_redeem (resolve winning positions at $1.00)
        We also check for expired positions (losers → $0).
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
                if side == "BUY":
                    buys.append(act)
                elif side == "SELL":
                    sells.append(act)
            elif t == "REDEEM":
                redeems.append(act)

        # Process: buys first (open), then sells (close at market price),
        # then redeems (close winners at $1.00)
        new_trades = 0
        for b in buys:
            self.sim_buy(b)
            new_trades += 1

        for s in sells:
            self.handle_sell(s)

        for r in redeems:
            self.handle_redeem(r)

        # Check for expired positions (losers)
        self.check_expired_positions(dolita_positions)

        return new_trades

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

        # Win rate on resolved positions
        resolved_wins = self.conn.execute(
            "SELECT COUNT(*) FROM sim_trades WHERE status='resolved' AND resolved_pnl > 0"
        ).fetchone()[0]

        return {
            "total_trades": total,
            "open": open_count,
            "resolved": resolved[0],
            "resolved_wins": resolved_wins,
            "realized_pnl": round(resolved[1], 2),
            "open_cost": round(cost_open, 2),
            "avail_balance": round(self._available_balance(), 2),
        }


# ─── Main ───────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ratio", type=float, default=0.03, help="Copy ratio vs dolita size (default: 3%%)")
    p.add_argument("--balance", type=float, default=700.0, help="Virtual balance (default: $700)")
    p.add_argument("--price-min", type=float, default=0.0, help="Min entry price to copy (default: 0)")
    p.add_argument("--price-max", type=float, default=1.0, help="Max entry price to copy (default: 1)")
    p.add_argument("--size-min", type=int, default=0, help="Min dolita shares to copy (default: 0)")
    p.add_argument("--size-max", type=int, default=999999, help="Max dolita shares to copy (default: unlimited)")
    args = p.parse_args()

    load_dotenv(dotenv_path=BASE_DIR / ".env")

    conn = init_db()
    trader = SimTrader(conn, balance=args.balance, ratio=args.ratio,
                       price_min=args.price_min, price_max=args.price_max,
                       size_min=args.size_min, size_max=args.size_max)

    # Build filter description
    filters = []
    if args.price_min > 0 or args.price_max < 1:
        filters.append(f"price ${args.price_min:.2f}-${args.price_max:.2f}")
    if args.size_min > 0 or args.size_max < 999999:
        filters.append(f"size {args.size_min}-{args.size_max} shares")
    filter_str = " | ".join(filters) if filters else "none (copy all)"

    logger.info("=" * 60)
    logger.info(f"🤖 Dolita SIM Copy Trader — Virtual ${args.balance:,.0f}")
    logger.info(f"   Target: dolita ({DOLITA_WALLET[:10]}...)")
    logger.info(f"   Strategy: BUY copy → SELL resolve (dolita's actual PnL)")
    logger.info(f"   Ratio: {args.ratio:.1%} | Poll: {POLL_INTERVAL}s")
    logger.info(f"   Filters: {filter_str}")
    logger.info(f"   DB: {DB_PATH}")
    logger.info("=" * 60)

    # Initialize: seed known IDs from extensive activity to avoid back-copying
    logger.info("Seeding known activity (skip historical)...")
    for offset in [0, 100, 200, 300, 400]:
        existing = fetch_dolita_activity(100)
        if not existing:
            break
        for act in existing:
            trader.known_ids.add(trader._event_id(act))
    count = len(trader.known_ids)
    trader._save_known()
    logger.info(f"  Seeded {count} existing events (will not simulate)")

    cycles = 0
    running = True

    def stop(sig, frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    while running:
        try:
            cycles += 1
            activities = fetch_dolita_activity(100)
            
            # Check positions every 3 cycles (~15s) for expired detection
            if cycles % 3 == 0:
                positions = fetch_dolita_positions()
            else:
                positions = []

            new_trades = trader.process_events(activities, positions)

            if new_trades > 0 or cycles % 12 == 0:  # log every ~60s or when there's activity
                s = trader.stats()
                win_rate = ""
                if s['resolved'] > 0:
                    win_rate = f" winRate={s['resolved_wins']/s['resolved']*100:.0f}%"
                logger.info(
                    f"📊 C#{cycles} | new={new_trades} | "
                    f"total={s['total_trades']} open={s['open']} resolved={s['resolved']}{win_rate} | "
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

    logger.info("🏁 Dolita sim bot stopped")


if __name__ == "__main__":
    main()
