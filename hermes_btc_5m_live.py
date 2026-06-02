"""
🎯 Hermes BTC 5M Live Trader — Real Polymarket Orders ($100 Capital)

Same v3 momentum strategy as the sim bot, but places real orders via CLOB API.
"""

import os, sys, json, time, signal, sqlite3, logging, argparse, subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"
for d in [LOG_DIR, DATA_DIR]: d.mkdir(parents=True, exist_ok=True)

HKT = timezone(timedelta(hours=8))
ET  = timezone(timedelta(hours=-4))

# ── Config ($100 capital) ────────────────────────────────────────
BINANCE_URL     = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
DATA_API        = "https://data-api.polymarket.com"
CLOB_API        = "https://clob.polymarket.com"
GAMMA_API       = "https://gamma-api.polymarket.com"
REF_WALLET      = "0x218f36c313d6c012636b21cc6b5a043d9fef17bc"
POLL_INTERVAL   = 4
TRADING_ONLY      = True        # Skip low-win-rate overnight window
SKIP_AFTER_HKT    = "22:30"     # Stop trading at Beijing 10:30 PM
SKIP_UNTIL_HKT    = "09:00"     # Resume at Beijing 9:00 AM next day
SKIP_AFTERNOON_FROM = "13:00"   # Also skip afternoon weak window
SKIP_AFTERNOON_TO   = "16:00"   # Resume at 4:00 PM
INITIAL_CAPITAL = 100.0
MAX_PER_MARKET  = 6.0       # max total per 5-min market
MIN_PER_MARKET  = 3.0       # min when on losing streak
TRADE_SIZE      = 3.0       # per-entry trade size (>=5 shares @ $0.50)
MIN_TRADE       = 2.00      # Polymarket min 5 shares × ~$0.40
MOMENTUM_WINDOWS = [10, 30, 60]   # seconds
MOMENTUM_WEIGHTS  = [0.5, 0.35, 0.15]
MOMENTUM_THRESH   = 0.03          # 0.03% = reduced noise
MAX_WEIGHT        = 0.90          # max 90/10 split
PHASE_ALLOC       = {0: 0.35, 1: 0.40, 2: 0.25}
WHALE_MIN_SHARES  = 2000     # whale threshold per holder/order
WHALE_BIAS_STRENGTH = 0.35   # how much whale signal biases weight (0-1)
POLYGONSCAN_API_KEY = os.getenv("POLYGONSCAN_API_KEY", "")
ETHERSCAN_V2      = "https://api.etherscan.io/v2/api"
CTF_CONTRACT      = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
DB_PATH = DATA_DIR / "hermes_btc_live.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "hermes_btc_live.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("hermes_live")


# ── API Helpers ──────────────────────────────────────────────────
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


def get_btc_price() -> Optional[float]:
    data = curl(BINANCE_URL)
    if data and "price" in data:
        return float(data["price"])
    return None


# ── Market Discovery (same as sim) ───────────────────────────────
def get_current_market_et() -> datetime:
    now_et = datetime.now(HKT).astimezone(ET)
    minute_block = (now_et.minute // 5) * 5
    return now_et.replace(minute=minute_block, second=0, microsecond=0)


def get_expected_slug(market_et: datetime) -> str:
    return f"btc-updown-5m-{int(market_et.timestamp())}"


def discover_market() -> Optional[dict]:
    market_et = get_current_market_et()
    expected_slug = get_expected_slug(market_et)

    # Generate candidate slugs: current + previous 3 (cover clock drift + late discovery)
    candidates = [expected_slug]
    for i in range(1, 4):
        candidates.append(get_expected_slug(market_et - timedelta(minutes=5 * i)))

    # Direct Gamma API lookup by slug — no reference wallet needed
    import httpx
    cid = None
    for slug in candidates:
        try:
            client = httpx.Client(http2=True, timeout=15)
            r = client.get(f"{GAMMA_API}/markets?slug={slug}&limit=1")
            client.close()
            data = r.json() if r.status_code == 200 else None
            if data and isinstance(data, list) and len(data) > 0:
                cid = data[0].get("conditionId")
                if cid:
                    break
        except Exception:
            continue
    if not cid:
        # Last resort: try cached CID from previous run
        cache_path = DATA_DIR / ".last_cid"
        if cache_path.exists():
            try:
                cached = cache_path.read_text().strip().split(",")
                if len(cached) == 2:
                    cached_cid, cached_ts = cached
                    if int(cached_ts) >= int(market_et.timestamp()) - 3600:
                        cid = cached_cid
            except Exception:
                pass
    if not cid:
        return None

    # Cache successful CID for next time
    try:
        (DATA_DIR / ".last_cid").write_text(f"{cid},{int(market_et.timestamp())}")
    except Exception:
        pass

    # Use httpx for CLOB API calls (bypasses GFW curl filtering)
    try:
        import httpx
        client = httpx.Client(http2=True, timeout=15)
        r = client.get(f"{CLOB_API}/markets/{cid}")
        client.close()
        clob_data = r.json() if r.status_code == 200 else None
    except Exception:
        clob_data = None
    if not clob_data or "tokens" not in clob_data:
        return None

    tokens = {}
    for t in clob_data["tokens"]:
        outcome = t.get("outcome", "")
        if outcome in ("Up", "Down"):
            tokens[outcome] = t["token_id"]
    if len(tokens) < 2:
        return None

    # ── Stale market detection: verify at least one orderbook is alive ──
    try:
        client2 = httpx.Client(http2=True, timeout=10)
        alive = False
        for outcome in ("Up", "Down"):
            token = tokens.get(outcome)
            if not token:
                continue
            try:
                r2 = client2.get(f"{CLOB_API}/book?token_id={token}")
                if r2.status_code == 200:
                    alive = True
                    break
            except Exception:
                pass
        client2.close()
        if not alive:
            # All orderbooks dead → market expired, invalidate cache
            cache_path = DATA_DIR / ".last_cid"
            if cache_path.exists():
                cache_path.unlink()
            logger.warning(f"  💀 Stale market {cid[:16]}... → cache cleared, skipping")
            return None
    except Exception:
        pass  # If verification fails, proceed with discovery anyway

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
    try:
        import httpx
        client = httpx.Client(http2=True, timeout=15)
        for outcome in ("Up", "Down"):
            token = market["tokens"].get(outcome)
            if not token:
                continue
            try:
                r = client.get(f"{CLOB_API}/book?token_id={token}")
                book = r.json() if r.status_code == 200 else None
            except Exception:
                book = None
            if book and "bids" in book and book["bids"]:
                best_bid = float(book["bids"][0]["price"])
                best_ask = float(book["asks"][0]["price"]) if book.get("asks") else 1.0
                # Use best_ask for buying — guaranteed fill (market-order equivalent)
                prices[outcome] = round(best_ask, 4)
            else:
                prices[outcome] = 0.5
        client.close()
    except Exception:
        prices = {"Up": 0.5, "Down": 0.5}
    return prices


# ── Whale Detection ───────────────────────────────────────────────
def get_whale_signal(token_up: str, token_down: str, condition_id: str, prices: dict) -> dict:
    """
    Detect whale positions from CLOB order book + data-api trades (wallet-level).
    Returns: {"direction": "Up"|"Down"|None, "up_top3": float, "down_top3": float,
               "up_max": float, "down_max": float, "confidence": 0-1, "source": str}
    CRITICAL: Filters out wallets that hold BOTH sides (hedgers/arbitrageurs).
    """
    from collections import defaultdict
    
    result = {"direction": None, "up_top3": 0, "down_top3": 0,
              "up_max": 0, "down_max": 0, "confidence": 0, "source": "none"}
    
    up_orders = []   # CLOB bid sizes
    down_orders = []
    up_holders_raw = defaultdict(float)  # wallet → net shares
    down_holders_raw = defaultdict(float)
    
    # ── 1. CLOB order book: large orders = whale intent (fast, always works) ──
    try:
        import httpx, threading
        books = {}
        
        def fetch_book(side, token):
            try:
                client = httpx.Client(http2=True, timeout=10)
                r = client.get(f"{CLOB_API}/book?token_id={token}")
                client.close()
                if r.status_code == 200:
                    books[side] = r.json()
            except Exception:
                pass
        
        threads = [
            threading.Thread(target=fetch_book, args=("Up", token_up)),
            threading.Thread(target=fetch_book, args=("Down", token_down)),
        ]
        for t in threads: t.start()
        for t in threads: t.join(timeout=12)
        
        for side in ("Up", "Down"):
            book = books.get(side, {})
            bids = book.get("bids", [])
            for bid in bids:
                size = float(bid.get("size", 0))
                if size >= WHALE_MIN_SHARES:
                    if side == "Up":
                        up_orders.append(size)
                    else:
                        down_orders.append(size)
    except Exception as e:
        logger.debug(f"Whale CLOB check: {e}")
    
    # ── 2. data-api trades: wallet-level positions with hedging filter ──
    if condition_id:
        try:
            for page in range(3):
                url = f"{DATA_API}/trades?condition_id={condition_id}&limit=100"
                r = subprocess.run(
                    ["curl", "-s", "--connect-timeout", "5", "--max-time", "10", url],
                    capture_output=True, text=True, timeout=12,
                )
                trades = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else []
                if not isinstance(trades, list) or not trades:
                    break
                
                for t in trades:
                    wallet = t.get("proxyWallet", "")
                    side = t.get("side", "")       # BUY / SELL
                    asset = t.get("asset", "")      # token ID
                    size = float(t.get("size", 0))
                    
                    if not wallet or not asset:
                        continue
                    
                    if asset == token_up:
                        if side == "BUY":
                            up_holders_raw[wallet] += size
                        elif side == "SELL":
                            up_holders_raw[wallet] -= size
                    elif asset == token_down:
                        if side == "BUY":
                            down_holders_raw[wallet] += size
                        elif side == "SELL":
                            down_holders_raw[wallet] -= size
        except Exception as e:
            logger.debug(f"Whale trades check: {e}")
    
    # ── 3. Apply hedging wallet filter ──
    # Wallets that hold BOTH Up AND Down are hedgers/arbitrageurs → EXCLUDE
    up_wallets = {w for w, s in up_holders_raw.items() if s > 0}
    down_wallets = {w for w, s in down_holders_raw.items() if s > 0}
    hedge_wallets = up_wallets & down_wallets
    
    if hedge_wallets:
        logger.debug(f"  🚫 Filtered {len(hedge_wallets)} hedging wallets")
        for w in hedge_wallets:
            up_holders_raw.pop(w, None)
            down_holders_raw.pop(w, None)
        result["source"] = "trades_filtered"
    
    # Convert to sorted list (only net-long wallets)
    up_holders = sorted(
        [(w, s) for w, s in up_holders_raw.items() if s > 0],
        key=lambda x: -x[1]
    )
    down_holders = sorted(
        [(w, s) for w, s in down_holders_raw.items() if s > 0],
        key=lambda x: -x[1]
    )
    
    # Merge trade-level holdings into orders list
    for _, shares in up_holders:
        up_orders.append(shares)
    for _, shares in down_holders:
        down_orders.append(shares)
    
    if up_holders or down_holders:
        if result["source"] == "none":
            result["source"] = "trades"
    
    # ── 4. Compute whale signal ──
    if not up_orders and not down_orders:
        return result
    
    up_orders.sort(reverse=True)
    down_orders.sort(reverse=True)
    
    result["up_top3"] = sum(up_orders[:3])
    result["down_top3"] = sum(down_orders[:3])
    result["up_max"] = up_orders[0] if up_orders else 0
    result["down_max"] = down_orders[0] if down_orders else 0
    
    up_has_whale = result["up_max"] >= WHALE_MIN_SHARES
    down_has_whale = result["down_max"] >= WHALE_MIN_SHARES
    
    if up_has_whale and down_has_whale:
        if result["up_top3"] > result["down_top3"]:
            result["direction"] = "Up"
        else:
            result["direction"] = "Down"
        total = result["up_top3"] + result["down_top3"]
        result["confidence"] = min(0.9, abs(result["up_top3"] - result["down_top3"]) / max(total, 1))
        if result["source"] == "none":
            result["source"] = "clob"
    elif up_has_whale:
        result["direction"] = "Up"
        result["confidence"] = 0.5
        if result["source"] == "none":
            result["source"] = "clob"
    elif down_has_whale:
        result["direction"] = "Down"
        result["confidence"] = 0.5
        if result["source"] == "none":
            result["source"] = "clob"
    
    return result


# ── Momentum Tracker (identical to sim) ──────────────────────────
class MomentumTracker:
    def __init__(self):
        self.prices: List[float] = []
        self.max_len = max(MOMENTUM_WINDOWS) + 5
        self.ticks = 0
        self.market_ticks = 0

    def reset_market(self):
        self.market_ticks = 0

    def feed(self, price: float):
        self.prices.append(price)
        if len(self.prices) > self.max_len:
            self.prices.pop(0)
        self.ticks += 1
        self.market_ticks += 1

    def momentum_at(self, window: int) -> float:
        if len(self.prices) < max(2, window):
            return 0.0
        recent = self.prices[-window:] if window <= len(self.prices) else self.prices
        if len(recent) < 2:
            return 0.0
        return (recent[-1] - recent[0]) / recent[0] * 100

    def composite_momentum(self) -> float:
        return sum(w * self.momentum_at(ws) for w, ws in zip(MOMENTUM_WEIGHTS, MOMENTUM_WINDOWS))

    def trend_strength(self) -> float:
        mom = abs(self.composite_momentum())
        return min(1.0, mom / (MOMENTUM_THRESH * 3))

    def trend_direction(self) -> str:
        mom = self.composite_momentum()
        if mom > MOMENTUM_THRESH:
            return "Up"
        elif mom < -MOMENTUM_THRESH:
            return "Down"
        return "Neutral"

    def weight_split(self) -> tuple:
        """Return (up_weight, down_weight). Strong trend → wider split."""
        mom = self.composite_momentum()
        trend = self.trend_direction()
        strength = min(abs(mom) / (MOMENTUM_THRESH * 2), 1.0)
        base = 0.5
        spread = (MAX_WEIGHT - base) * strength

        if trend == "Up":
            return (base + spread, base - spread)
        elif trend == "Down":
            return (base - spread, base + spread)
        return (base, base)

    def phase(self) -> int:
        """0=EARLY (<15s), 1=MID (15-30s), 2=LATE (>30s)"""
        t = self.market_ticks * POLL_INTERVAL
        if t < 15:
            return 0
        elif t < 30:
            return 1
        return 2

    def phase_budget(self, base_max: float) -> float:
        p = self.phase()
        cum_alloc = sum(PHASE_ALLOC[i] for i in range(p + 1))
        budget = base_max * cum_alloc
        # Ensure at least one trade can be placed (prevents deadlock on losing streak)
        if p == 0 and budget < MIN_TRADE:
            budget = MIN_TRADE
        return budget


# ── Database ─────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS markets (
            slug TEXT PRIMARY KEY, title TEXT, condition_id TEXT,
            token_up TEXT, token_down TEXT, et_start TEXT, et_end TEXT,
            total_up_cost REAL DEFAULT 0, total_down_cost REAL DEFAULT 0,
            up_shares REAL DEFAULT 0, down_shares REAL DEFAULT 0,
            resolved INTEGER DEFAULT 0, result TEXT,
            payout REAL DEFAULT 0, pnl REAL DEFAULT 0,
            trend_strength REAL DEFAULT 0, max_weight_used REAL DEFAULT 0.5
        );
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, market_slug TEXT NOT NULL,
            side TEXT NOT NULL, price REAL NOT NULL,
            shares REAL NOT NULL, cost REAL NOT NULL,
            order_id TEXT, btc_price REAL,
            up_weight REAL, trend_strength REAL, phase INTEGER,
            resolved INTEGER DEFAULT 0, pnl REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, balance REAL NOT NULL, total_pnl REAL
        );
        CREATE TABLE IF NOT EXISTS win_streak (
            id INTEGER PRIMARY KEY, streak INTEGER DEFAULT 0,
            total_wins INTEGER DEFAULT 0, total_losses INTEGER DEFAULT 0
        );
    """)
    conn.execute("INSERT OR IGNORE INTO win_streak (id, streak, total_wins, total_losses) VALUES (1, 0, 0, 0)")
    conn.commit()
    return conn


def get_streak(conn) -> int:
    row = conn.execute("SELECT streak FROM win_streak WHERE id=1").fetchone()
    return row[0] if row else 0


# ── Live Trader ───────────────────────────────────────────────────
class LiveTrader:
    def __init__(self, balance: float, dry_run: bool = False):
        self.balance = balance
        self.initial = balance
        self.dry_run = dry_run
        self.momentum = MomentumTracker()
        self.current_market: Optional[dict] = None
        self.traded_slugs = set()
        self.positions: Dict[str, dict] = {}
        self.win_streak = 0
        self.clob_client = None

    def init_clob(self):
        """Initialize Polymarket CLOB client."""
        from py_clob_client_v2 import ClobClient, ApiCreds

        private_key = os.getenv("POLY_PRIVATE_KEY", "")
        api_key = os.getenv("POLY_API_KEY", "")
        api_secret = os.getenv("POLY_API_SECRET", "")
        api_passphrase = os.getenv("POLY_API_PASSPHRASE", "")
        proxy_wallet = os.getenv("POLY_PROXY_WALLET", os.getenv("POLY_DEPOSIT_WALLET", ""))

        if not all([private_key, api_key, api_secret, api_passphrase]):
            raise ValueError("Missing POLY_* env vars. Check .env file.")

        api_creds = ApiCreds(
            api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase
        )
        self.clob_client = ClobClient(
            host=CLOB_API, chain_id=137, key=private_key,
            creds=api_creds, funder=proxy_wallet, signature_type=3,
            retry_on_error=True, use_server_time=True,
        )
        logger.info(f"CLOB client initialized")

    def get_real_balance(self) -> Optional[float]:
        """Query Polymarket for actual USDC balance. Returns float or None."""
        if not self.clob_client or self.dry_run:
            return None
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)

        for attempt in range(3):
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(self.clob_client.get_balance_allowance, params=params)
                    bal = future.result(timeout=20)
                raw = bal.get("balance", 0)
                return float(raw) / 1e6
            except (FutureTimeout, Exception) as e:
                if attempt < 2:
                    import time as _t; _t.sleep(3)
                    logger.warning(f"Balance retry {attempt+1}/2: {e}")
                else:
                    logger.warning(f"Balance query failed: {e}")
                    return None

    def recalibrate_balance(self):
        """Sync internal balance with real wallet balance."""
        real = self.get_real_balance()
        if real is not None:
            drift = real - self.balance
            if abs(drift) > 0.01:
                logger.info(f"⚖️ Recalibrate: {self.balance:.2f} → {real:.2f} (drift {drift:+.2f})")
                self.balance = real

    def total_pnl(self) -> float:
        return self.balance - self.initial

    def adaptive_max(self) -> float:
        streak = self.win_streak
        if streak >= 3:   return MAX_PER_MARKET * 1.25
        elif streak >= 1:  return MAX_PER_MARKET * 1.10
        elif streak <= -3: return MIN_PER_MARKET
        elif streak <= -1: return MAX_PER_MARKET * 0.85
        return MAX_PER_MARKET

    def place_real_order(self, side: str, price: float, shares: int, token_id: str) -> Optional[str]:
        """Place a real order on Polymarket. Returns order_id or None."""
        from py_clob_client_v2 import OrderArgs, PartialCreateOrderOptions
        from py_clob_client_v2.order_builder.constants import BUY

        price = round(float(price), 2)
        actual_cost = round(shares * price, 2)

        if self.dry_run:
            logger.info(f"  🎫 DRY: {side} {shares}sh @ ${price:.2f} = ${actual_cost:.2f}")
            return f"dry_{side}_{int(time.time())}"

        if not self.clob_client:
            logger.error("CLOB client not initialized")
            return None

        try:
            response = self.clob_client.create_and_post_order(
                OrderArgs(token_id=token_id, price=price, size=shares, side=BUY),
                options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
            )
            oid = response.get("orderID") or response.get("id", "")
            logger.info(f"  ✅ REAL ORDER: {side} {shares}sh @ ${price:.2f} = ${actual_cost:.2f} | id={str(oid)[:20]}")
            return str(oid)
        except Exception as e:
            err_str = str(e)
            # Don't retry when orderbook is gone (market expired)
            if "does not exist" in err_str or "400" in err_str:
                logger.warning(f"  ⏰ ORDERBOOK GONE ({side}): market expired, skipping")
                return None
            logger.warning(f"  ⚠️ ORDER RETRY 1/3: {side} @ ${price:.2f}: {e}")
            import time as _time
            for attempt in range(2, 4):
                try:
                    _time.sleep(3)
                    response = self.clob_client.create_and_post_order(
                        OrderArgs(token_id=token_id, price=price, size=shares, side=BUY),
                        options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
                    )
                    oid = response.get("orderID") or response.get("id", "")
                    logger.info(f"  ✅ REAL ORDER (retry{attempt}): {side} {shares}sh @ ${price:.2f} = ${actual_cost:.2f} | id={str(oid)[:20]}")
                    return str(oid)
                except Exception as e2:
                    err2_str = str(e2)
                    if "does not exist" in err2_str or "400" in err2_str:
                        logger.warning(f"  ⏰ ORDERBOOK GONE ({side}): market expired, skipping")
                        return None
                    logger.warning(f"  ⚠️ ORDER RETRY {attempt}/3: {side} @ ${price:.2f}: {e2}")
            logger.error(f"  ❌ ORDER FAILED (3/3): {side} @ ${price:.2f}")
            return None

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
        logger.info(f"   BTC=${btc_price:,.0f} | Bal=${self.balance:.2f} | Max={max_budget:.0f} | Streak={streak:+d}")

    def tick(self, btc_price: float, prices: dict) -> List[dict]:
        self.momentum.feed(btc_price)
        if not self.current_market:
            return []

        up_w, down_w = self.momentum.weight_split()
        mom = self.momentum.composite_momentum()
        trend = self.momentum.trend_strength()
        phase = self.momentum.phase()
        trend_dir = self.momentum.trend_direction()

        # ── Whale signal integration ──
        whale = get_whale_signal(
            self.current_market["tokens"].get("Up", ""),
            self.current_market["tokens"].get("Down", ""),
            self.current_market.get("condition_id", ""),
            prices,
        )
        whale_dir = whale.get("direction")
        whale_conf = whale.get("confidence", 0)
        whale_source = whale.get("source", "none")

        if whale_dir and whale_conf >= 0.3:
            w_up_top3 = whale.get("up_top3", 0)
            w_down_top3 = whale.get("down_top3", 0)
            w_up_max = whale.get("up_max", 0)
            w_down_max = whale.get("down_max", 0)

            if trend_dir == "Neutral":
                # Whale signal overrides inertia — follow the whales
                if whale_dir == "Up":
                    up_w, down_w = 0.75, 0.25
                else:
                    up_w, down_w = 0.25, 0.75
                trend_dir = whale_dir  # pretend momentum agrees
                logger.info(
                    f"  🐋 WHALE OVERRIDE: {whale_dir} | "
                    f"Up Top3={w_up_top3:,.0f} Down Top3={w_down_top3:,.0f} | "
                    f"conf={whale_conf:.0%} src={whale_source}"
                )
            elif whale_dir != trend_dir:
                # Whale opposes momentum → blend
                bias = WHALE_BIAS_STRENGTH * whale_conf
                if whale_dir == "Up":
                    up_w = up_w * (1 - bias) + 0.75 * bias
                    down_w = 1.0 - up_w
                else:
                    down_w = down_w * (1 - bias) + 0.75 * bias
                    up_w = 1.0 - down_w
                logger.info(
                    f"  🐋 WHALE CONTRARY: momentum={trend_dir} whale={whale_dir} | "
                    f"bias={bias:.0%} → w={up_w:.0%}/{down_w:.0%} | "
                    f"Up Top3={w_up_top3:,.0f} Down Top3={w_down_top3:,.0f}"
                )
            elif whale_conf >= 0.5:
                # Whale agrees with momentum → reinforce
                if whale_dir == "Up":
                    up_w = min(MAX_WEIGHT, up_w + 0.05)
                    down_w = 1.0 - up_w
                else:
                    down_w = min(MAX_WEIGHT, down_w + 0.05)
                    up_w = 1.0 - down_w
                logger.info(
                    f"  🐋 WHALE CONFIRMS: {whale_dir} | "
                    f"Up Top3={w_up_top3:,.0f} Down Top3={w_down_top3:,.0f} | "
                    f"→ w={up_w:.0%}/{down_w:.0%}"
                )

        # Skip neutral markets — no clear direction signal
        if trend_dir == "Neutral":
            return []

        slug = self.current_market["slug"]
        pos = self.positions.get(slug)
        if not pos:
            return []

        max_budget = self.adaptive_max()
        spent = pos["up_cost"] + pos["down_cost"]
        phase_budget = self.momentum.phase_budget(max_budget)
        remaining = phase_budget - spent
        if remaining < MIN_TRADE:
            return []

        up_price = prices.get("Up", 0.5)
        down_price = prices.get("Down", 0.5)

        if up_price > 0.90:
            down_w = 0
            remaining = (phase_budget - spent) * (up_w / max(up_w, 0.01))
        elif down_price > 0.90:
            up_w = 0
            remaining = (phase_budget - spent) * (down_w / max(down_w, 0.01))

        phase_names = ["EARLY", "MID", "LATE"]
        arrow = "⬆️" if trend_dir == "Up" else ("⬇️" if trend_dir == "Down" else "↔️")
        logger.info(
            f"  {arrow} BTC mom={mom:+.3f}% trend={trend:.0%} | "
            f"w={up_w:.0%}/{down_w:.0%} | {phase_names[phase]} ${spent:.0f}/${phase_budget:.0f}"
        )

        # Fix: in neutral (50/50) momentum, per-side budget < MIN_TRADE
        # but total budget is enough → allocate full remaining to dominant side
        if remaining >= MIN_TRADE:
            up_chunk = remaining * up_w
            down_chunk = remaining * down_w
            if up_chunk < MIN_TRADE and down_chunk < MIN_TRADE:
                if up_w >= down_w:
                    up_w, down_w = 1.0, 0.0
                else:
                    up_w, down_w = 0.0, 1.0

        trades = []
        for side, weight, price in [("Up", up_w, up_price), ("Down", down_w, down_price)]:
            if weight <= 0 or price <= 0 or price >= 1:
                continue
            budget = remaining * weight
            if budget < MIN_TRADE:
                continue
            est_cost = min(TRADE_SIZE, budget)
            if est_cost < MIN_TRADE:
                continue
            shares = max(5, int(est_cost / price))
            actual_cost = round(shares * price, 2)
            if actual_cost > self.balance:
                continue

            # Place REAL order
            token_id = self.current_market["tokens"].get(side)
            oid = self.place_real_order(side, price, shares, token_id) if token_id else None

            if oid:
                self.balance -= actual_cost
                if side == "Up":
                    pos["up_cost"] += actual_cost
                    pos["up_shares"] += shares
                else:
                    pos["down_cost"] += actual_cost
                    pos["down_shares"] += shares
                trades.append({"side": side, "price": price, "shares": shares, "cost": actual_cost, "order_id": oid})
            # else: order failed — do NOT record, do NOT subtract balance

        return trades

    def close_market(self, slug: str, condition_id: str, conn: sqlite3.Connection):
        """Resolve market by querying Polymarket for actual outcome."""
        pos = self.positions.get(slug, {})
        up_cost = pos.get("up_cost", 0)
        down_cost = pos.get("down_cost", 0)
        up_shares = pos.get("up_shares", 0)
        down_shares = pos.get("down_shares", 0)

        # Query real outcome from Polymarket
        result = self.resolve_market(condition_id)
        logger.info(f"  🔍 Market {slug[-12:]} resolved as: {result}")

        if result == "Up":
            payout = up_shares * 0.98  # 2% buffer for real
            pnl = payout - up_cost - down_cost
        elif result == "Down":
            payout = down_shares * 0.98
            pnl = payout - up_cost - down_cost
        else:
            # Unknown or unsettled — just return costs, no PnL yet
            logger.info(f"  ⏳ Market not yet resolved, will retry")
            return False  # not settled

        self.balance += (up_cost + down_cost + pnl)
        won = pnl > 0

        conn.execute(
            "UPDATE markets SET resolved=1, result=?, payout=?, pnl=?, "
            "up_shares=?, down_shares=?, total_up_cost=?, total_down_cost=? WHERE slug=?",
            (result, round(payout, 2), round(pnl, 2),
             up_shares, down_shares, round(up_cost, 2), round(down_cost, 2), slug),
        )
        # Settle individual trades: winning side gets proportional payout, losing side = -cost
        if result == "Up":
            conn.execute(
                "UPDATE trades SET resolved=1, pnl=? WHERE market_slug=? AND side='Up' AND resolved=0",
                (round(up_shares * 0.98 - up_cost, 2), slug),
            )
            conn.execute(
                "UPDATE trades SET resolved=1, pnl=ROUND(-cost,2) WHERE market_slug=? AND side='Down' AND resolved=0",
                (slug,),
            )
        else:
            conn.execute(
                "UPDATE trades SET resolved=1, pnl=ROUND(-cost,2) WHERE market_slug=? AND side='Up' AND resolved=0",
                (slug,),
            )
            conn.execute(
                "UPDATE trades SET resolved=1, pnl=? WHERE market_slug=? AND side='Down' AND resolved=0",
                (round(down_shares * 0.98 - down_cost, 2), slug),
            )
        conn.commit()

        # Update streak
        streak_row = conn.execute("SELECT * FROM win_streak WHERE id=1").fetchone()
        if streak_row:
            total_w = streak_row[2] + (1 if won else 0)
            total_l = streak_row[3] + (0 if won else 1)
            conn.execute(
                "UPDATE win_streak SET streak=?, total_wins=?, total_losses=? WHERE id=1",
                (self.win_streak + (1 if won else -1), total_w, total_l),
            )
            conn.commit()

        emoji = "🟢 WIN" if won else "🔴 LOSS"
        logger.info(f"🏁 {emoji} {slug[-12:]}: {result} | PnL=${pnl:+.2f} | Bal=${self.balance:.2f}")
        return True

    def resolve_market(self, condition_id: str) -> str:
        """Query Polymarket CLOB for market resolution. Returns 'Up', 'Down', or 'Unknown'."""
        if not condition_id:
            return "Unknown"

        import httpx, time as _t
        for attempt in range(3):
            try:
                client = httpx.Client(http2=True, timeout=15)
                r = client.get(f"{CLOB_API}/markets/{condition_id}")
                client.close()
                data = r.json() if r.status_code == 200 else None
            except Exception as e:
                data = None
                if attempt < 2:
                    _t.sleep(3)

            if not data:
                continue

            tokens = data.get("tokens", [])
            if data.get("closed") or data.get("accepting_orders") == False:
                for token in tokens:
                    outcome = token.get("outcome", "")
                    if token.get("winner", False) or token.get("price", 0) >= 0.99:
                        if outcome in ("Up", "Down"):
                            return outcome

            # Market exists but not resolved yet — don't retry, just return Unknown
            break

        return "Unknown"


# ── Trading Hours ────────────────────────────────────────────────
def is_trading_hours() -> bool:
    """Skip Beijing 22:30-09:00 (overnight) and 13:00-16:00 (afternoon weak window)."""
    if not TRADING_ONLY:
        return True
    now = datetime.now(HKT)
    h, m = int(SKIP_AFTER_HKT.split(":")[0]), int(SKIP_AFTER_HKT.split(":")[1])
    uh, um = int(SKIP_UNTIL_HKT.split(":")[0]), int(SKIP_UNTIL_HKT.split(":")[1])
    # Overnight: skip if after stop time OR before start time
    if now.hour > h or (now.hour == h and now.minute >= m):
        return False
    if now.hour < uh or (now.hour == uh and now.minute < um):
        return False
    # Afternoon weak window: 13:00-16:00
    #     ah, am = int(SKIP_AFTERNOON_FROM.split(":")[0]), int(SKIP_AFTERNOON_FROM.split(":")[1])
    #     bh, bm = int(SKIP_AFTERNOON_TO.split(":")[0]), int(SKIP_AFTERNOON_TO.split(":")[1])
    #     if (now.hour > ah or (now.hour == ah and now.minute >= am)) and \
    #        (now.hour < bh or (now.hour == bh and now.minute < bm)):
    #         return False
    return True


# ── Main ─────────────────────────────────────────────────────────
def main():
    load_dotenv(dotenv_path=BASE_DIR / ".env")

    parser = argparse.ArgumentParser(description="Hermes BTC 5M Live Trader")
    parser.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    parser.add_argument("--dry-run", action="store_true", help="Simulate, no real orders")
    parser.add_argument("--db", type=str, default=None, help="Database path (default: hermes_btc_live.db)")
    parser.add_argument("--no-sleep", action="store_true", help="Disable trading hours sleep (run 24/7)")
    args = parser.parse_args()

    # Allow overriding DB path for sim vs live
    global DB_PATH, TRADING_ONLY
    if args.db:
        DB_PATH = Path(args.db) if args.db.startswith("/") else DATA_DIR / args.db
    if args.no_sleep:
        TRADING_ONLY = False

    balance = args.capital
    conn = init_db()
    streak = get_streak(conn)
    trader = LiveTrader(balance=balance, dry_run=args.dry_run)

    if not args.dry_run:
        trader.init_clob()
        # Sync to real wallet balance
        real_bal = trader.get_real_balance()
        if real_bal is not None and real_bal > 0:
            logger.info(f"💰 Real wallet: ${real_bal:.2f} (override --capital ${balance:.0f})")
            trader.balance = real_bal
            trader.initial = real_bal
            balance = real_bal

        # Re-resolve Unknown markets from previous runs
        unknown_markets = conn.execute(
            "SELECT slug, condition_id, up_shares, down_shares, total_up_cost, total_down_cost "
            "FROM markets WHERE resolved=1 AND result='Unknown'"
        ).fetchall()
        if unknown_markets:
            logger.info(f"🔁 Re-resolving {len(unknown_markets)} Unknown markets...")
            fixed = 0
            for u_slug, u_cid, up_shares, down_shares, up_cost, down_cost in unknown_markets:
                up_shares = up_shares or 0
                down_shares = down_shares or 0
                up_cost = up_cost or 0
                down_cost = down_cost or 0
                result = trader.resolve_market(u_cid)
                if result in ("Up", "Down"):
                    if result == "Up":
                        payout = up_shares * 0.98
                    else:
                        payout = down_shares * 0.98
                    pnl = payout - up_cost - down_cost
                    conn.execute(
                        "UPDATE markets SET result=?, payout=?, pnl=? WHERE slug=?",
                        (result, round(payout, 2), round(pnl, 2), u_slug),
                    )
                    # Settle trades
                    if result == "Up":
                        conn.execute(
                            "UPDATE trades SET resolved=1, pnl=? WHERE market_slug=? AND side='Up' AND resolved=0",
                            (round(up_shares * 0.98 - up_cost, 2), u_slug),
                        )
                        conn.execute(
                            "UPDATE trades SET resolved=1, pnl=ROUND(-cost,2) WHERE market_slug=? AND side='Down' AND resolved=0",
                            (u_slug,),
                        )
                    else:
                        conn.execute(
                            "UPDATE trades SET resolved=1, pnl=ROUND(-cost,2) WHERE market_slug=? AND side='Up' AND resolved=0",
                            (u_slug,),
                        )
                        conn.execute(
                            "UPDATE trades SET resolved=1, pnl=? WHERE market_slug=? AND side='Down' AND resolved=0",
                            (round(down_shares * 0.98 - down_cost, 2), u_slug),
                        )
                    fixed += 1
            conn.commit()
            logger.info(f"🔁 Fixed {fixed}/{len(unknown_markets)} Unknown → re-resolved")
    else:
        logger.info("🔍 DRY RUN MODE — no real orders will be placed")

    logger.info("=" * 55)
    logger.info(f"🎯 Hermes BTC 5M Live — {'🔍 DRY RUN' if args.dry_run else '💸 LIVE TRADING'}")
    logger.info(f"   Capital: ${balance:.0f} | Base max/market: ${MAX_PER_MARKET:.0f}")
    logger.info(f"   Momentum: {MOMENTUM_WINDOWS}s | Threshold: {MOMENTUM_THRESH:.2f}%")
    logger.info(f"   Max split: {MAX_WEIGHT:.0%} | Phases: {PHASE_ALLOC}")
    logger.info("=" * 55)

    conn.execute(
        "INSERT INTO snapshots (timestamp, balance, total_pnl) VALUES (?,?,0)",
        (datetime.now(HKT).isoformat(), balance),
    )
    conn.commit()

    last_slug = None
    last_cid = None
    cycles = 0
    running = True
    pending_resolve = []  # (slug, cid) pairs awaiting resolution
    stale_count = 0       # consecutive cycles with no valid market found

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
                stale_count = 0  # reset on success
                slug = market["slug"]

                if slug != last_slug:
                    if last_slug and last_cid:
                        settled = trader.close_market(last_slug, last_cid, conn)
                        if not settled:
                            pending_resolve.append((last_slug, last_cid))
                    streak = get_streak(conn)
                    trader.open_market(market, btc, streak)
                    last_cid = market.get("condition_id", "")

                    conn.execute(
                        """INSERT OR IGNORE INTO markets
                           (slug, title, condition_id, token_up, token_down, et_start, et_end)
                           VALUES (?,?,?,?,?,?,?)""",
                        (slug, market["title"], market["condition_id"],
                         market["tokens"].get("Up", ""), market["tokens"].get("Down", ""),
                         market["et_start"], market["et_end"]),
                    )
                    conn.commit()
                    last_slug = slug

                prices = get_current_prices(market)
                if prices:
                    new_trades = trader.tick(btc, prices)
                    now_ts = datetime.now(HKT).isoformat()
                    for t in new_trades:
                        conn.execute(
                            """INSERT INTO trades
                               (timestamp, market_slug, side, price, shares, cost,
                                order_id, btc_price, up_weight, trend_strength, phase)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (now_ts, slug, t["side"], t["price"], t["shares"], t["cost"],
                             t.get("order_id", ""), btc,
                             trader.momentum.weight_split()[0],
                             trader.momentum.trend_strength(),
                             trader.momentum.phase()),
                        )
                    conn.commit()

            # Periodic balance recalibration (every ~5 min)
            if cycles % 75 == 0 and not args.dry_run:
                trader.recalibrate_balance()

            # Force-clear CID cache if stuck too long with no valid market
            if not market:
                stale_count += 1
                if stale_count >= 20:  # ~80 seconds without a valid market
                    cache_path = DATA_DIR / ".last_cid"
                    if cache_path.exists():
                        cache_path.unlink()
                        logger.warning("🧹 Stale for 20 cycles → cleared CID cache")
                    stale_count = 0

            # Periodic logging and retry pending resolutions
            if cycles % 30 == 0 and pending_resolve:
                still_pending = []
                for p_slug, p_cid in pending_resolve:
                    if trader.close_market(p_slug, p_cid, conn):
                        logger.info(f"  ✅ Retry resolved: {p_slug[-12:]}")
                    else:
                        still_pending.append((p_slug, p_cid))
                pending_resolve = still_pending

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
