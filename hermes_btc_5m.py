"""
🎯 Hermes BTC 5-Minute Binary Options Bot v3 — Optimized

Key improvements over v2:
  1. Multi-timeframe momentum (10s / 30s / 60s)
  2. Trend detection with conviction scoring
  3. Adaptive position sizing based on win streak
  4. Staggered entries (3 phases per market)
  5. Wider weight spread (90/10 max)
  6. More sensitive threshold (0.03%)
  7. Smaller trade size ($4) for better averaging
"""

import os, sys, json, time, signal, sqlite3, logging, argparse, subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"
for d in [LOG_DIR, DATA_DIR]: d.mkdir(parents=True, exist_ok=True)

HKT = timezone(timedelta(hours=8))
ET  = timezone(timedelta(hours=-4))

# ── Config ──────────────────────────────────────────────────────
REF_WALLET      = "0x218f36c313d6c012636b21cc6b5a043d9fef17bc"
POLL_INTERVAL   = 4          # seconds
TRADING_ONLY      = True        # Skip low-win-rate overnight window
SKIP_AFTER_HKT    = "22:30"     # Stop trading at Beijing 10:30 PM
SKIP_UNTIL_HKT    = "09:00"     # Resume at Beijing 9:00 AM next day
MAX_PER_MARKET  = 20.0       # base max per market (adaptive ±25%)
MIN_PER_MARKET  = 15.0
TRADE_SIZE      = 4.0        # smaller trades, more averaging
MIN_TRADE       = 1.0
MAX_WEIGHT      = 0.90       # max 90% on one side (was 80%)
SIM_BALANCE     = 700.0

# Multi-timeframe momentum
MOMENTUM_WINDOWS = [10, 30, 60]       # seconds
MOMENTUM_WEIGHTS = [0.5, 0.35, 0.15]  # recent weighted more
MOMENTUM_THRESH  = 0.03               # more sensitive (was 0.05)

# Phase-based entry (% of max_per_market per phase)
PHASE_ALLOC = {0: 0.35, 1: 0.40, 2: 0.25}  # early=35%, mid=40%, late=25%
PHASE_BOUNDARIES = [90, 210]  # seconds into 5-min window
WHALE_MIN_SHARES  = 2000
WHALE_BIAS_STRENGTH = 0.35
POLYGONSCAN_API_KEY = os.getenv("POLYGONSCAN_API_KEY", "")
ETHERSCAN_V2      = "https://api.etherscan.io/v2/api"
CTF_CONTRACT      = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

BINANCE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
DATA_API    = "https://data-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"
GAMMA_API   = "https://gamma-api.polymarket.com"

DB_PATH = DATA_DIR / "hermes_btc_sim.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "hermes_btc_5m.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("hermes_btc")


# ================================================================
# HTTP
# ================================================================
def curl(url: str) -> Optional[dict]:
    try:
        r = subprocess.run(
            ["/usr/bin/curl", "-s", "--connect-timeout", "3", "--max-time", "10", url],
            capture_output=True, text=True, timeout=12,
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except:
        pass
    return None


# ================================================================
# MARKET DISCOVERY
# ================================================================
def get_current_market_et() -> datetime:
    now_et = datetime.now(HKT).astimezone(ET)
    minute_block = (now_et.minute // 5) * 5
    return now_et.replace(minute=minute_block, second=0, microsecond=0)


def get_expected_slug(market_et: datetime) -> str:
    return f"btc-updown-5m-{int(market_et.timestamp())}"


def discover_market() -> Optional[dict]:
    market_et = get_current_market_et()
    expected_slug = get_expected_slug(market_et)
    
    # Direct Gamma API lookup — no reference wallet needed
    cid = None
    data = curl(f"{GAMMA_API}/markets?slug={expected_slug}&limit=1")
    if data and isinstance(data, list) and len(data) > 0:
        cid = data[0].get("conditionId")
    if not cid:
        return None
    
    clob_data = curl(f"{CLOB_API}/markets/{cid}")
    if not clob_data or "tokens" not in clob_data:
        return None
    
    tokens = {}
    for t in clob_data["tokens"]:
        outcome = t.get("outcome", "")
        if outcome in ("Up", "Down"):
            tokens[outcome] = t["token_id"]
    if len(tokens) < 2:
        return None
    
    return {
        "slug": expected_slug,
        "condition_id": cid,
        "title": clob_data.get("question", expected_slug),
        "tokens": tokens,
        "et_start": market_et.isoformat(),
        "et_end": (market_et + timedelta(minutes=5)).isoformat(),
    }


def get_current_prices(market: dict) -> dict:
    prices = {}
    for outcome in ("Up", "Down"):
        token = market["tokens"].get(outcome)
        if not token:
            continue
        book = curl(f"{CLOB_API}/book?token_id={token}")
        if book and "bids" in book and book["bids"]:
            best_bid = float(book["bids"][0]["price"])
            best_ask = float(book["asks"][0]["price"]) if book.get("asks") else 1.0
            mid = (best_bid + best_ask) / 2 if best_bid > 0 else best_ask
            prices[outcome] = round(mid, 4)
        else:
            prices[outcome] = 0.5
    return prices


# ================================================================
# BTC PRICE
# ================================================================
def get_btc_price() -> Optional[float]:
    data = curl(BINANCE_URL)
    if data and "price" in data:
        try:
            return float(data["price"])
        except:
            pass
    return None


# ================================================================
# ENHANCED MOMENTUM + TREND DETECTION
# ================================================================
class MomentumTracker:
    """Multi-timeframe momentum with trend detection."""
    
    def __init__(self):
        self.history: List[tuple] = []  # [(timestamp, price), ...]
        self.ticks_up = 0
        self.ticks_down = 0
        self.last_price = None
        self.market_start_time = 0  # set when market opens
    
    def reset_market(self):
        """Reset per-market state."""
        self.market_start_time = time.time()
    
    def feed(self, price: float):
        now = time.time()
        self.history.append((now, price))
        cutoff = now - 300
        self.history = [(t, p) for t, p in self.history if t > cutoff]
        
        # Track directional ticks
        if self.last_price is not None:
            if price > self.last_price:
                self.ticks_up += 1
            elif price < self.last_price:
                self.ticks_down += 1
        self.last_price = price
    
    def momentum(self, window: int = 30) -> float:
        """% change over given window in seconds."""
        if len(self.history) < 2:
            return 0.0
        cutoff = time.time() - window
        old = [p for t, p in self.history if t <= cutoff]
        old_price = old[-1] if old else self.history[0][1]
        cur = self.history[-1][1]
        if old_price == 0:
            return 0.0
        return (cur - old_price) / old_price * 100
    
    def composite_momentum(self) -> float:
        """Weighted multi-timeframe momentum."""
        total = 0.0
        for window, weight in zip(MOMENTUM_WINDOWS, MOMENTUM_WEIGHTS):
            total += self.momentum(window) * weight
        return total
    
    def trend_strength(self) -> float:
        """Trend conviction: 0=no trend, 1=strong trend.
        
        Based on: (a) ratio of up/down ticks, (b) consistency of direction.
        """
        total_ticks = self.ticks_up + self.ticks_down
        if total_ticks < 3:
            return 0.0
        
        # Dominance ratio: 0.5 = balanced, 1.0 = all one way
        dominant = max(self.ticks_up, self.ticks_down)
        ratio = dominant / total_ticks
        
        # Scale: 0.5 → 0.0, 1.0 → 1.0
        strength = (ratio - 0.5) * 2
        return max(0.0, min(1.0, strength))
    
    def trend_direction(self) -> str:
        """Returns 'Up', 'Down', or 'None'."""
        if self.ticks_up > self.ticks_down * 1.5:
            return "Up"
        elif self.ticks_down > self.ticks_up * 1.5:
            return "Down"
        return "None"
    
    def weight_split(self) -> tuple:
        """Calculate (up_weight, down_weight) using composite momentum + trend.
        
        Strong trend → wider split. Weak trend → closer to 50/50.
        """
        mom = self.composite_momentum()
        trend = self.trend_strength()
        
        # Base shift from momentum
        base_shift = mom / MOMENTUM_THRESH * 0.08
        
        # Amplify by trend strength: +50% when trending
        shift = base_shift * (1.0 + trend * 0.5)
        
        # Clamp
        max_shift = MAX_WEIGHT - 0.5
        shift = max(-max_shift, min(max_shift, shift))
        
        up_w = 0.50 + shift
        up_w = max(1 - MAX_WEIGHT, min(MAX_WEIGHT, up_w))
        return up_w, 1.0 - up_w
    
    def phase(self) -> int:
        """Return current phase: 0=early, 1=mid, 2=late."""
        if self.market_start_time == 0:
            return 0
        elapsed = time.time() - self.market_start_time
        if elapsed < PHASE_BOUNDARIES[0]:
            return 0
        elif elapsed < PHASE_BOUNDARIES[1]:
            return 1
        return 2
    
    def phase_budget(self, base_max: float) -> float:
        """Max budget allowed so far based on current phase."""
        p = self.phase()
        cum_alloc = sum(PHASE_ALLOC[i] for i in range(p + 1))
        return base_max * cum_alloc


# ================================================================
# DATABASE
# ================================================================
def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS markets (
            slug TEXT PRIMARY KEY,
            title TEXT,
            condition_id TEXT,
            token_up TEXT,
            token_down TEXT,
            et_start TEXT,
            et_end TEXT,
            total_up_cost REAL DEFAULT 0,
            total_down_cost REAL DEFAULT 0,
            up_shares REAL DEFAULT 0,
            down_shares REAL DEFAULT 0,
            resolved INTEGER DEFAULT 0,
            result TEXT,
            payout REAL DEFAULT 0,
            pnl REAL DEFAULT 0,
            trend_strength REAL DEFAULT 0,
            max_weight_used REAL DEFAULT 0.5
        );
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            market_slug TEXT NOT NULL,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            shares REAL NOT NULL,
            cost REAL NOT NULL,
            btc_price REAL,
            up_weight REAL,
            down_weight REAL,
            trend_strength REAL,
            phase INTEGER,
            resolved INTEGER DEFAULT 0,
            resolution TEXT,
            payout REAL DEFAULT 0,
            pnl REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            balance REAL NOT NULL,
            total_pnl REAL
        );
        CREATE TABLE IF NOT EXISTS win_streak (
            id INTEGER PRIMARY KEY,
            streak INTEGER DEFAULT 0,
            total_wins INTEGER DEFAULT 0,
            total_losses INTEGER DEFAULT 0
        );
    """)
    # Ensure streak row exists
    conn.execute("INSERT OR IGNORE INTO win_streak (id, streak, total_wins, total_losses) VALUES (1, 0, 0, 0)")
    conn.commit()
    return conn


def get_streak(conn) -> int:
    row = conn.execute("SELECT streak FROM win_streak WHERE id=1").fetchone()
    return row[0] if row else 0


def update_streak(conn, won: bool):
    if won:
        conn.execute("UPDATE win_streak SET streak = MAX(0, streak) + 1, total_wins = total_wins + 1 WHERE id=1")
    else:
        conn.execute("UPDATE win_streak SET streak = MIN(0, streak) - 1, total_losses = total_losses + 1 WHERE id=1")
    conn.commit()


# ================================================================
# SIM TRADER v3
# ================================================================
class SimTrader:
    def __init__(self, balance: float = SIM_BALANCE):
        self.balance = balance
        self.initial = balance
        self.momentum = MomentumTracker()
        self.current_market: Optional[dict] = None
        self.traded_slugs = set()
        self.positions: Dict[str, dict] = {}
        self.win_streak = 0
    
    def total_pnl(self) -> float:
        return self.balance - self.initial
    
    def adaptive_max(self) -> float:
        """Adjust max per market based on win streak.
        
        Winning → scale up (max +25%). Losing → scale down (max -25%).
        """
        streak = self.win_streak
        if streak >= 3:
            return MAX_PER_MARKET * 1.25
        elif streak >= 1:
            return MAX_PER_MARKET * 1.10
        elif streak <= -3:
            return MIN_PER_MARKET
        elif streak <= -1:
            return MAX_PER_MARKET * 0.85
        return MAX_PER_MARKET
    
    def open_market(self, market: dict, btc_price: float, streak: int):
        slug = market["slug"]
        if slug in self.traded_slugs:
            return
        self.current_market = market
        self.momentum.reset_market()
        self.win_streak = streak
        self.positions[slug] = {"up_cost": 0, "down_cost": 0, "up_shares": 0, "down_shares": 0}
        
        max_budget = self.adaptive_max()
        title_short = market['title'].replace('Bitcoin Up or Down - ', '')[:40]
        logger.info(f"🆕 {title_short}")
        logger.info(f"   BTC=${btc_price:,.0f} | Bal=${self.balance:.2f} | Max=${max_budget:.0f} | Streak={streak:+d}")
    
    def tick(self, btc_price: float, prices: dict) -> List[dict]:
        self.momentum.feed(btc_price)
        if not self.current_market:
            return []
        
        up_w, down_w = self.momentum.weight_split()
        mom = self.composite_momentum_display()
        trend = self.momentum.trend_strength()
        phase = self.momentum.phase()
        trend_dir = self.momentum.trend_direction()
        
        slug = self.current_market["slug"]
        pos = self.positions.get(slug)
        if not pos:
            return []
        
        # Adaptive budget
        max_budget = self.adaptive_max()
        spent = pos["up_cost"] + pos["down_cost"]
        phase_budget = self.momentum.phase_budget(max_budget)
        remaining = phase_budget - spent
        if remaining < MIN_TRADE:
            return []
        
        up_price = prices.get("Up", 0.5)
        down_price = prices.get("Down", 0.5)
        
        # Stop buying if one side is clearly winning (>0.90)
        if up_price > 0.90:
            down_w = 0
            remaining = phase_budget - spent
            if remaining > 0:
                remaining = remaining * (up_w / max(up_w, 0.01))
        elif down_price > 0.90:
            up_w = 0
            remaining = phase_budget - spent
            if remaining > 0:
                remaining = remaining * (down_w / max(down_w, 0.01))
        
        # Trend indicators for log
        trend_icon = "🔥" if trend > 0.5 else ("📈" if trend > 0.2 else "➡️")
        phase_names = ["EARLY", "MID", "LATE"]
        arrow = "⬆️" if trend_dir == "Up" else ("⬇️" if trend_dir == "Down" else "↔️")
        
        logger.info(
            f"  {arrow} BTC mom={mom:+.3f}% trend={trend:.0%} {trend_icon} | "
            f"w={up_w:.0%}/{down_w:.0%} | {phase_names[phase]} ${spent:.0f}/${phase_budget:.0f}"
        )
        
        trades = []
        for side, weight, price in [("Up", up_w, up_price), ("Down", down_w, down_price)]:
            if weight <= 0 or price <= 0 or price >= 1:
                continue
            budget = remaining * weight
            if budget < MIN_TRADE:
                continue
            cost = min(TRADE_SIZE, budget)
            if cost < MIN_TRADE or cost > self.balance:
                continue
            shares = cost / price
            
            self.balance -= cost
            if side == "Up":
                pos["up_cost"] += cost
                pos["up_shares"] += shares
            else:
                pos["down_cost"] += cost
                pos["down_shares"] += shares
            
            trades.append({"side": side, "price": price, "shares": shares, "cost": cost})
            logger.info(f"  🎫 {side:4s}: {shares:.1f}sh @ ${price:.4f} = ${cost:.2f}")
        
        if trades:
            self.traded_slugs.add(slug)
            self._save_trades(trades, slug, btc_price, up_w, trend, phase)
        
        return trades
    
    def composite_momentum_display(self) -> float:
        """Momentum for display (30s window)."""
        return self.momentum.momentum(30)
    
    def close_market(self, slug: str, result: str, conn):
        pos = self.positions.get(slug)
        if not pos:
            return
        
        if result == "Up":
            payout = pos["up_shares"] * 1.0
        else:
            payout = pos["down_shares"] * 1.0
        
        total_cost = pos["up_cost"] + pos["down_cost"]
        pnl = payout - total_cost
        won = pnl > 0
        self.balance += payout
        
        emoji = "✅" if won else "❌"
        logger.info(f"🏁 {emoji} {slug[-12:]}: {result} | Cost=${total_cost:.2f} Payout=${payout:.2f} PnL=${pnl:+.2f}")
        
        conn.execute(
            "UPDATE markets SET resolved=1, result=?, payout=?, pnl=?, total_up_cost=?, total_down_cost=?, up_shares=?, down_shares=? WHERE slug=?",
            (result, payout, pnl, pos["up_cost"], pos["down_cost"], pos["up_shares"], pos["down_shares"], slug),
        )
        conn.execute(
            "UPDATE trades SET resolved=1, payout=CASE WHEN side=? THEN shares ELSE 0 END, pnl=CASE WHEN side=? THEN shares-cost ELSE -cost END WHERE market_slug=?",
            (result, result, slug),
        )
        update_streak(conn, won)
        conn.commit()
        
        if self.current_market and self.current_market["slug"] == slug:
            self.current_market = None
    
    def _save_trades(self, trades: list, slug: str, btc_price: float, up_w: float, trend: float, phase: int):
        conn = sqlite3.connect(str(DB_PATH))
        now = datetime.now(HKT).isoformat()
        for t in trades:
            conn.execute(
                "INSERT INTO trades (timestamp, market_slug, side, price, shares, cost, btc_price, up_weight, down_weight, trend_strength, phase) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (now, slug, t["side"], t["price"], t["shares"], t["cost"], btc_price, up_w, 1-up_w, trend, phase),
            )
        conn.commit()
        conn.close()


# ================================================================
# MAIN
# ================================================================

def is_trading_hours():
    """Skip Beijing 22:30-09:00 (proven low-win-rate overnight window)."""
    if not TRADING_ONLY:
        return True
    now = datetime.now(HKT)
    h, m = int(SKIP_AFTER_HKT.split(":")[0]), int(SKIP_AFTER_HKT.split(":")[1])
    uh, um = int(SKIP_UNTIL_HKT.split(":")[0]), int(SKIP_UNTIL_HKT.split(":")[1])
    if now.hour > h or (now.hour == h and now.minute >= m):
        return False
    if now.hour < uh or (now.hour == uh and now.minute < um):
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Hermes BTC 5M Bot v3")
    parser.add_argument("--balance", type=float, default=SIM_BALANCE)
    args = parser.parse_args()
    
    balance = args.balance
    conn = init_db()
    streak = get_streak(conn)
    trader = SimTrader(balance=balance)
    
    logger.info("=" * 55)
    logger.info(f"🎯 Hermes BTC 5M Bot v3 — 🔍 SIM (optimized)")
    logger.info(f"   Balance: ${balance:.0f} | Base max/market: ${MAX_PER_MARKET:.0f}")
    logger.info(f"   Momentum: {MOMENTUM_WINDOWS}s windows | Threshold: {MOMENTUM_THRESH:.2f}%")
    logger.info(f"   Weights: {MOMENTUM_WEIGHTS} | Max split: {MAX_WEIGHT:.0%}")
    logger.info(f"   Phases: {PHASE_ALLOC} | Streak: {streak:+d}")
    logger.info("=" * 55)
    
    conn.execute(
        "INSERT INTO snapshots (timestamp, balance, total_pnl) VALUES (?,?,0)",
        (datetime.now(HKT).isoformat(), balance),
    )
    conn.commit()
    
    last_slug = None
    cycles = 0
    running = True
    
    signal.signal(signal.SIGINT, lambda *a: None)
    signal.signal(signal.SIGTERM, lambda *a: None)
    
    while running:
        try:
            cycles += 1

            if not is_trading_hours():
                if cycles % 75 == 0:
                    now_hkt = datetime.now(HKT)
                    logger.info(f"🌙 休眠时段 (北京 {now_hkt.hour:02d}:{now_hkt.minute:02d}) — {SKIP_AFTER_HKT}~{SKIP_UNTIL_HKT} 历史胜率低，跳过")
                time.sleep(POLL_INTERVAL)
                continue

            btc = get_btc_price()
            if btc is None:
                time.sleep(2)
                continue
            
            market = discover_market()
            if market:
                slug = market["slug"]
                
                if slug != last_slug:
                    if last_slug:
                        logger.info(f"⏰ Window ended: {last_slug[-12:]}")
                    streak = get_streak(conn)
                    trader.open_market(market, btc, streak)
                    
                    conn.execute(
                        """INSERT OR IGNORE INTO markets 
                           (slug, title, condition_id, token_up, token_down, et_start, et_end)
                           VALUES (?,?,?,?,?,?,?)""",
                        (slug, market["title"], market["condition_id"],
                         market["tokens"].get("Up",""), market["tokens"].get("Down",""),
                         market["et_start"], market["et_end"]),
                    )
                    conn.commit()
                    last_slug = slug
                
                prices = get_current_prices(market)
                if prices:
                    trader.tick(btc, prices)
            
            # Resolution check
            if cycles % 15 == 0:
                now = datetime.now(HKT)
                rows = conn.execute(
                    "SELECT slug FROM markets WHERE resolved=0 ORDER BY et_end ASC LIMIT 20"
                ).fetchall()
                for (s,) in rows:
                    row2 = conn.execute("SELECT et_end FROM markets WHERE slug=?", (s,)).fetchone()
                    if not row2:
                        continue
                    try:
                        et_end = datetime.fromisoformat(row2[0])
                        if now < et_end.astimezone(HKT) + timedelta(minutes=2):
                            continue
                    except:
                        continue
                    
                    row3 = conn.execute(
                        "SELECT condition_id FROM markets WHERE slug=?", (s,)
                    ).fetchone()
                    if not row3:
                        continue
                    cid = row3[0]
                    
                    clob = curl(f"{CLOB_API}/markets/{cid}")
                    if clob and clob.get("tokens"):
                        for t in clob["tokens"]:
                            if t.get("winner") is True:
                                result = t.get("outcome", "")
                                if result in ("Up", "Down"):
                                    trader.close_market(s, result, conn)
                                break
            
            if cycles % 60 == 0:
                pnl = trader.total_pnl()
                streak = get_streak(conn)
                logger.info(f"📊 C#{cycles} | BTC=${btc:,.0f} | Bal=${trader.balance:.2f} | PnL=${pnl:+.2f} | Streak={streak:+d}")
                conn.execute(
                    "INSERT INTO snapshots (timestamp, balance, total_pnl) VALUES (?,?,?)",
                    (datetime.now(HKT).isoformat(), trader.balance, pnl),
                )
                conn.commit()
            
            time.sleep(POLL_INTERVAL)
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Cycle error: {e}")
            time.sleep(10)
    
    conn.close()
    logger.info("🏁 Bot stopped")


if __name__ == "__main__":
    main()
