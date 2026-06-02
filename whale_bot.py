#!/usr/bin/env python3
"""
🐋 Whale Bot — Pure Top-Holder Signal Strategy (Sim)

Strategy: Each 5-min BTC market, check top 3 Up and Down holders.
Filter out bilateral hedging wallets. Follow the side with stronger whale signal.

Usage:
  python3 whale_bot.py                    # sim with $200 default
  python3 whale_bot.py --balance 500      # custom balance
  python3 whale_bot.py --bet-size 5       # $5 per bet
"""

import os, sys, json, time, signal, sqlite3, logging, argparse, subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Optional, Dict, List
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"
for d in [LOG_DIR, DATA_DIR]: d.mkdir(parents=True, exist_ok=True)

HKT = timezone(timedelta(hours=8))
ET  = timezone(timedelta(hours=-4))

# ── Config ────────────────────────────────────────────────────────
GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"
DATA_API    = "https://data-api.polymarket.com"
POLL_INTERVAL  = 6          # seconds between market checks
WHALE_MIN_SHARES = 2000     # whale threshold per holder/order
WHALE_MIN_CONF  = 0.25      # minimum confidence to bet
MAX_PER_MARKET  = 5.0       # max bet per market
TRADE_START_HKT = "09:00"
TRADE_END_HKT   = "22:30"

DB_PATH = DATA_DIR / "whale_bot.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "whale_bot.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("whale_bot")


# ── DB ────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bet_at TEXT NOT NULL,
            market_slug TEXT NOT NULL,
            market_title TEXT,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            shares REAL NOT NULL,
            cost REAL NOT NULL,
            whale_dir TEXT,
            whale_conf REAL,
            up_top3 REAL DEFAULT 0,
            down_top3 REAL DEFAULT 0,
            status TEXT DEFAULT 'open',
            resolved_pnl REAL DEFAULT 0,
            resolved_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn


# ── Market Discovery ──────────────────────────────────────────────
def get_current_market_et() -> datetime:
    now_et = datetime.now(HKT).astimezone(ET)
    minute_block = (now_et.minute // 5) * 5
    return now_et.replace(minute=minute_block, second=0, microsecond=0)


def get_expected_slug(market_et: datetime) -> str:
    return f"btc-updown-5m-{int(market_et.timestamp())}"


def discover_market() -> Optional[dict]:
    market_et = get_current_market_et()
    expected_slug = get_expected_slug(market_et)

    candidates = [expected_slug]
    for i in range(1, 4):
        candidates.append(get_expected_slug(market_et - timedelta(minutes=5 * i)))

    cid = None
    for slug in candidates:
        try:
            import httpx
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
        cache_path = DATA_DIR / ".whale_cid"
        if cache_path.exists():
            try:
                cached = cache_path.read_text().strip().split(",")
                if len(cached) == 2 and int(cached[1]) >= int(market_et.timestamp()) - 3600:
                    cid = cached[0]
            except Exception:
                pass
    if not cid:
        return None

    try:
        (DATA_DIR / ".whale_cid").write_text(f"{cid},{int(market_et.timestamp())}")
    except Exception:
        pass

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

    # Stale check
    try:
        client2 = httpx.Client(http2=True, timeout=10)
        alive = False
        for token in tokens.values():
            try:
                r2 = client2.get(f"{CLOB_API}/book?token_id={token}")
                if r2.status_code == 200:
                    alive = True
                    break
            except Exception:
                pass
        client2.close()
        if not alive:
            cache_path = DATA_DIR / ".whale_cid"
            if cache_path.exists():
                cache_path.unlink()
            return None
    except Exception:
        pass

    return {
        "slug": expected_slug,
        "condition_id": cid,
        "title": clob_data.get("question", expected_slug),
        "tokens": tokens,
        "et_start": market_et.isoformat(),
        "et_end": (market_et + timedelta(minutes=5)).isoformat(),
    }


def get_mid_prices(market: dict) -> dict:
    """Get mid prices for both tokens (best_bid+best_ask)/2."""
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
            if book and book.get("bids") and book.get("asks"):
                best_bid = float(book["bids"][0]["price"])
                best_ask = float(book["asks"][0]["price"])
                prices[outcome] = round((best_bid + best_ask) / 2, 4)
            else:
                prices[outcome] = 0.5
        client.close()
    except Exception:
        prices = {"Up": 0.5, "Down": 0.5}
    return prices


# ── Whale Signal ──────────────────────────────────────────────────
def get_whale_signal(token_up: str, token_down: str, condition_id: str) -> dict:
    """
    Detect whale positions per side. Filter bilateral hedgers.
    Returns: {direction, up_top3, down_top3, up_max, down_max, confidence, source}
    """
    result = {"direction": None, "up_top3": 0, "down_top3": 0,
              "up_max": 0, "down_max": 0, "confidence": 0, "source": "none"}

    up_orders = []
    down_orders = []
    up_holders_raw = defaultdict(float)
    down_holders_raw = defaultdict(float)

    # 1. CLOB order book
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
            for bid in book.get("bids", []):
                size = float(bid.get("size", 0))
                if size >= WHALE_MIN_SHARES:
                    (up_orders if side == "Up" else down_orders).append(size)
    except Exception as e:
        logger.debug(f"Whale CLOB: {e}")

    # 2. data-api trades: wallet-level with hedging filter
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
                    side = t.get("side", "")
                    asset = t.get("asset", "")
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
            logger.debug(f"Whale trades: {e}")

    # 3. Filter hedging wallets
    up_wallets = {w for w, s in up_holders_raw.items() if s > 0}
    down_wallets = {w for w, s in down_holders_raw.items() if s > 0}
    hedge_wallets = up_wallets & down_wallets

    if hedge_wallets:
        logger.debug(f"  🚫 Filtered {len(hedge_wallets)} hedging wallets")
        for w in hedge_wallets:
            up_holders_raw.pop(w, None)
            down_holders_raw.pop(w, None)
        result["source"] = "trades_filtered"

    up_holders = sorted([(w, s) for w, s in up_holders_raw.items() if s > 0], key=lambda x: -x[1])
    down_holders = sorted([(w, s) for w, s in down_holders_raw.items() if s > 0], key=lambda x: -x[1])

    for _, shares in up_holders:
        up_orders.append(shares)
    for _, shares in down_holders:
        down_orders.append(shares)

    if up_holders or down_holders:
        if result["source"] == "none":
            result["source"] = "trades"

    # 4. Compute signal
    if not up_orders and not down_orders:
        return result

    up_orders.sort(reverse=True)
    down_orders.sort(reverse=True)

    result["up_top3"] = sum(up_orders[:3])
    result["down_top3"] = sum(down_orders[:3])
    result["up_max"] = up_orders[0] if up_orders else 0
    result["down_max"] = down_orders[0] if down_orders else 0

    up_has = result["up_max"] >= WHALE_MIN_SHARES
    down_has = result["down_max"] >= WHALE_MIN_SHARES

    if up_has and down_has:
        if result["up_top3"] > result["down_top3"]:
            result["direction"] = "Up"
        else:
            result["direction"] = "Down"
        total = result["up_top3"] + result["down_top3"]
        result["confidence"] = min(0.9, abs(result["up_top3"] - result["down_top3"]) / max(total, 1))
        if result["source"] == "none":
            result["source"] = "clob"
    elif up_has:
        result["direction"] = "Up"
        result["confidence"] = 0.5
        if result["source"] == "none":
            result["source"] = "clob"
    elif down_has:
        result["direction"] = "Down"
        result["confidence"] = 0.5
        if result["source"] == "none":
            result["source"] = "clob"

    return result


# ── Sim Trader ────────────────────────────────────────────────────
class WhaleSimTrader:
    def __init__(self, conn: sqlite3.Connection, balance: float = 200.0, bet_size: float = 5.0):
        self.conn = conn
        self.initial_balance = balance
        self.bet_size = bet_size
        self.bet_markets: set = set()  # already bet on these slugs this round
        self._load_bet_markets()

    def _load_bet_markets(self):
        row = self.conn.execute("SELECT value FROM state WHERE key='bet_markets'").fetchone()
        if row:
            try:
                self.bet_markets = set(json.loads(row[0]))
            except Exception:
                pass

    def _save_bet_markets(self):
        self.conn.execute(
            "INSERT OR REPLACE INTO state (key, value) VALUES ('bet_markets', ?)",
            (json.dumps(list(self.bet_markets)[-500:]),),
        )
        self.conn.commit()

    def available_balance(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost), 0) FROM trades WHERE status='open'"
        ).fetchone()
        return max(0, self.initial_balance - row[0])

    def place_bet(self, market: dict, prices: dict, whale: dict):
        """Place a simulated bet based on whale signal."""
        slug = market["slug"]

        # Don't double-bet the same market
        if slug in self.bet_markets:
            return

        direction = whale.get("direction")
        confidence = whale.get("confidence", 0)

        if not direction or confidence < WHALE_MIN_CONF:
            return

        price = prices.get(direction, 0.5)
        avail = self.available_balance()

        if avail < self.bet_size:
            return

        bet = min(self.bet_size, avail, MAX_PER_MARKET)
        shares = max(1, int(bet / price)) if price > 0 else 1
        cost = round(shares * price, 2)

        self.conn.execute(
            """INSERT INTO trades
               (bet_at, market_slug, market_title, side, price, shares, cost,
                whale_dir, whale_conf, up_top3, down_top3, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
            (datetime.now(HKT).isoformat(), slug, market.get("title", slug)[:80],
             direction, price, shares, cost,
             direction, round(confidence, 4),
             whale.get("up_top3", 0), whale.get("down_top3", 0)),
        )
        self.conn.commit()
        self.bet_markets.add(slug)
        self._save_bet_markets()

        logger.info(
            f"🐋 BET {direction:>4s} | {shares}sh @ ${price:.4f} | "
            f"cost=${cost:.2f} | conf={confidence:.0%} | "
            f"U3={whale.get('up_top3',0):,.0f} D3={whale.get('down_top3',0):,.0f}"
        )

    def resolve_market(self, market: dict):
        """Check if a market resolved and update PnL."""
        slug = market["slug"]
        rows = self.conn.execute(
            "SELECT id, side, shares, cost FROM trades WHERE market_slug=? AND status='open'",
            (slug,),
        ).fetchall()

        if not rows:
            return

        try:
            import httpx
            client = httpx.Client(http2=True, timeout=10)
            books = {}
            for outcome in ("Up", "Down"):
                token = market["tokens"].get(outcome)
                if not token:
                    continue
                try:
                    r = client.get(f"{CLOB_API}/book?token_id={token}")
                    books[outcome] = r.json() if r.status_code == 200 else None
                except Exception:
                    books[outcome] = None
            client.close()

            # Determine winner from orderbook imbalance
            winner = None
            for outcome in ("Up", "Down"):
                book = books.get(outcome)
                if not book:
                    continue
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                other = "Down" if outcome == "Up" else "Up"

                # Case 1: bid_only near $1.00 → this side WON
                if bids and not asks:
                    best_bid = float(bids[0]["price"])
                    if best_bid >= 0.97:
                        winner = outcome
                        break
                # Case 2: ask_only near $1.00 → this side LOST (other side WON)
                if asks and not bids:
                    best_ask = float(asks[0]["price"])
                    if best_ask >= 0.97:
                        winner = other
                        break
                # Case 3: bid_only near $0.00 → this side LOST
                if bids and not asks:
                    best_bid = float(bids[0]["price"])
                    if best_bid <= 0.01:
                        winner = other
                        break
                # Case 4: both sides alive → check mid for extreme
                if bids and asks:
                    mid = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
                    if mid >= 0.98:
                        winner = outcome
                        break
                    if mid <= 0.02:
                        winner = other
                        break

            if not winner:
                return

            logger.info(f"🏁 RESOLVED: {slug[-20:]} → {winner}")

            for tid, side, shares, cost in rows:
                if side == winner:
                    pnl = round(shares - cost, 4)  # $1.00 payout per share
                else:
                    pnl = round(-cost, 4)  # total loss
                self.conn.execute(
                    "UPDATE trades SET status='resolved', resolved_pnl=?, resolved_at=? WHERE id=?",
                    (pnl, datetime.now(HKT).isoformat(), tid),
                )
                emoji = "🟢" if pnl > 0 else "🔴"
                logger.info(f"  {emoji} #{tid}: {side} → {winner} | PnL=${pnl:+.2f}")
            self.conn.commit()
        except Exception as e:
            logger.debug(f"Resolve check error: {e}")

    def resolve_all_open(self):
        """Periodic check: resolve all open trades by looking up each market."""
        slugs = self.conn.execute(
            "SELECT DISTINCT market_slug FROM trades WHERE status='open'"
        ).fetchall()
        if not slugs:
            return
        for (slug,) in slugs:
            try:
                self._resolve_by_slug(slug)
            except Exception:
                pass

    def _resolve_by_slug(self, slug: str):
        """Resolve trades for a specific market slug."""
        rows = self.conn.execute(
            "SELECT id, side, shares, cost FROM trades WHERE market_slug=? AND status='open'",
            (slug,),
        ).fetchall()
        if not rows:
            return

        # Try Gamma API to get tokens
        try:
            import httpx
            client = httpx.Client(http2=True, timeout=15)
            r = client.get(f"{GAMMA_API}/markets?slug={slug}&limit=1")
            client.close()
            if r.status_code != 200:
                return
            data = r.json()
            if not data or not isinstance(data, list) or len(data) == 0:
                return
            m = data[0]
            tokens = {}
            for t in m.get('tokens', []):
                outcome = t.get('outcome', '')
                if outcome in ('Up', 'Down'):
                    tokens[outcome] = t.get('tokenId', '')
            if len(tokens) < 2:
                return
        except Exception:
            return

        # Check orderbooks
        books = {}
        for outcome, tid in tokens.items():
            try:
                c2 = httpx.Client(http2=True, timeout=10)
                r2 = c2.get(f"{CLOB_API}/book?token_id={tid}")
                c2.close()
                books[outcome] = r2.json() if r2.status_code == 200 else None
            except Exception:
                books[outcome] = None

        winner = self._determine_winner(books)
        if not winner:
            return

        logger.info(f"🏁 RESOLVED (periodic): {slug[-20:]} → {winner}")
        self._apply_resolution(rows, winner)

    def _determine_winner(self, books: dict) -> Optional[str]:
        """Determine market winner from orderbooks."""
        for outcome in ("Up", "Down"):
            book = books.get(outcome)
            if not book:
                continue
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            other = "Down" if outcome == "Up" else "Up"

            if bids and not asks:
                bb = float(bids[0]["price"])
                if bb >= 0.97:
                    return outcome
                if bb <= 0.01:
                    return other
            if asks and not bids:
                ba = float(asks[0]["price"])
                if ba >= 0.97:
                    return other
            if bids and asks:
                mid = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
                if mid >= 0.98:
                    return outcome
                if mid <= 0.02:
                    return other
        return None

    def _apply_resolution(self, rows, winner: str):
        """Apply resolution result to trades."""
        for tid, side, shares, cost in rows:
            if side == winner:
                pnl = round(shares - cost, 4)
            else:
                pnl = round(-cost, 4)
            self.conn.execute(
                "UPDATE trades SET status='resolved', resolved_pnl=?, resolved_at=? WHERE id=?",
                (pnl, datetime.now(HKT).isoformat(), tid),
            )
            emoji = "🟢" if pnl > 0 else "🔴"
            logger.info(f"  {emoji} #{tid}: {side} → {winner} | PnL=${pnl:+.2f}")
        self.conn.commit()

    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        open_n = self.conn.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(resolved_pnl),0) FROM trades WHERE status='resolved'"
        ).fetchone()
        cost_open = self.conn.execute(
            "SELECT COALESCE(SUM(cost),0) FROM trades WHERE status='open'"
        ).fetchone()[0]
        avail = self.available_balance()
        return {
            "total": total, "open": open_n,
            "resolved": row[0], "realized_pnl": round(row[1], 2),
            "open_cost": round(cost_open, 2),
            "avail": round(avail, 2),
            "total_assets": round(avail + row[1], 2),
        }


# ── Trading Window ────────────────────────────────────────────────
def in_trading_window() -> bool:
    now = datetime.now(HKT).strftime("%H:%M")
    return TRADE_START_HKT <= now < TRADE_END_HKT


# ── Main ──────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="🐋 Whale Bot — Top-Holder Signal Sim")
    p.add_argument("--balance", type=float, default=200.0, help="Virtual balance")
    p.add_argument("--bet-size", type=float, default=5.0, help="Bet size per signal")
    p.add_argument("--24-7", action="store_true", help="Run 24/7, ignore trading window")
    args = p.parse_args()

    load_dotenv(dotenv_path=BASE_DIR / ".env")

    conn = init_db()
    trader = WhaleSimTrader(conn, balance=args.balance, bet_size=args.bet_size)

    logger.info("=" * 60)
    logger.info(f"🐋 WHALE BOT — Pure Top-Holder Signal")
    logger.info(f"   Balance: ${args.balance:.0f} | Bet: ${args.bet_size:.0f}")
    logger.info(f"   Min Confidence: {WHALE_MIN_CONF:.0%} | Whale Threshold: {WHALE_MIN_SHARES:,} sh")
    logger.info(f"   Window: {TRADE_START_HKT}-{TRADE_END_HKT} HKT")
    logger.info(f"   DB: {DB_PATH}")
    logger.info("=" * 60)

    cycles = 0
    last_market_slug = None
    prev_market_data = None   # store previous market for resolution
    running = True
    signal.signal(signal.SIGINT, lambda *a: setattr(sys.modules[__name__], "running", False))

    while running:
        try:
            cycles += 1
            in_window = args.__dict__.get("24_7", False) or in_trading_window()

            # ── Discover market ──
            market = discover_market()
            if not market:
                time.sleep(POLL_INTERVAL)
                continue

            slug = market["slug"]

            # New market → resolve previous, then start fresh
            if slug != last_market_slug:
                if prev_market_data:
                    trader.resolve_market(prev_market_data)
                trader.bet_markets.clear()
                trader._save_bet_markets()
                last_market_slug = slug
                prev_market_data = None

            # Store current market data for later resolution
            prev_market_data = dict(market)

            # ── Periodic resolve: check all open trades every 2 min ──
            if cycles % 20 == 0:
                trader.resolve_all_open()

            if not in_window:
                if cycles % 30 == 0:
                    logger.debug(f"🌙 Outside trading window ({datetime.now(HKT).strftime('%H:%M')})")
                time.sleep(POLL_INTERVAL)
                continue

            # ── Get prices & whale signal ──
            prices = get_mid_prices(market)
            whale = get_whale_signal(
                market["tokens"]["Up"],
                market["tokens"]["Down"],
                market["condition_id"],
            )

            # ── Bet if signal is strong ──
            if whale["confidence"] >= WHALE_MIN_CONF:
                trader.place_bet(market, prices, whale)

            # ── Stats every 2 minutes ──
            if cycles % 20 == 0:
                s = trader.stats()
                logger.info(
                    f"📊 #{cycles} | bets={s['total']} open={s['open']} "
                    f"resolved={s['resolved']} PnL=${s['realized_pnl']:+.2f} "
                    f"avail=${s['avail']:.2f} assets=${s['total_assets']:.2f}"
                )

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Cycle error: {e}")
            time.sleep(10)

    logger.info("🐋 Whale bot stopped")
    conn.close()


if __name__ == "__main__":
    main()
