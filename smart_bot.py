"""
🎯 Bandit Bot — Single-Sided Price Band Trading

Strategy: For each crypto up/down market (BTC, ETH, XRP, SOL),
buy cheap tokens, sell expensive. No pairing required.
Inspired by top Polymarket trader @0xcE25E214.

Core logic:
  - Scan ALL crypto 5m + 15m markets for cheap bids
  - When price dips below threshold → BUY
  - When price rises to target → SELL for profit
  - Stop loss on sharp drops
  - Multi-asset diversification for more opportunities

Usage:
  python3 smart_bot.py                  # Dry run
  python3 smart_bot.py --live           # Live trading  
  python3 smart_bot.py --live --size 3  # $3 per trade
"""

import os, sys, json, time, signal, logging, argparse, subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple, Set
from dataclasses import dataclass, field

from dotenv import load_dotenv
from py_clob_client_v2 import ClobClient, ApiCreds
from py_clob_client_v2.clob_types import (
    OrderArgsV2, OrderType, BalanceAllowanceParams, AssetType,
)
from py_clob_client_v2.order_builder.constants import BUY, SELL
from trade_journal import init_db, log_trade, log_balance

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"
for d in [LOG_DIR, DATA_DIR]:
    d.mkdir(parents=True, exist_ok=True)

GAMMA   = "https://gamma-api.polymarket.com"
CLOB    = "https://clob.polymarket.com"

# === Strategy Parameters ===
ASSETS = ["btc", "eth", "xrp", "sol"]       # all crypto markets
TIMEFRAMES = [5, 15]                         # 5m and 15m

BUY_CHEAP   = 0.38   # buy when price < this (absolute threshold)
BUY_DISCOUNT = 0.10  # also buy when price drops this much from mid
SELL_TARGET = 1.08   # sell when price >= entry * 1.08 (+8%)
STOP_LOSS   = 0.70   # sell when price <= entry * 0.70 (-30%)
MIN_PRICE   = 0.08   # don't trade tokens below this
MAX_HOLD    = 120     # auto-sell after 120 seconds regardless

DEFAULT_SIZE   = 5.0    # $ per trade (Polymarket minimum)
SCAN_INTERVAL  = 2.0    # seconds between market scans
MAX_POSITIONS  = 10     # max concurrent positions
MAX_OPEN_ORDERS = 16    # max concurrent buy+ sell orders
MIN_BALANCE    = 5.0    # don't trade below this
MIN_ORDER_VALUE = 1.0   # Polymarket minimum order value ($1)

CURL = ["curl", "-s", "--connect-timeout", "3", "--max-time", "8"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "bandit_bot.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("bandit")


# ============================================================
# UTILS
# ============================================================
def curl(url: str) -> Optional[dict]:
    try:
        r = subprocess.run(CURL + [url], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except:
        pass
    return None

def now_utc(): return datetime.now(timezone.utc)


# ============================================================
# ORDER BOOK SCANNER
# ============================================================
def get_book(token_id: str) -> Optional[dict]:
    """Get order book for a token."""
    return curl(f"{CLOB}/book?token_id={token_id}")


def get_market_price(token_id: str) -> Optional[float]:
    """Get mid market price quickly."""
    d = curl(f"{CLOB}/midpoint?token_id={token_id}")
    if d and "mid" in d:
        return float(d["mid"])
    # Fallback: use last trade price
    d2 = curl(f"{CLOB}/last-trade-price?token_id={token_id}")
    if d2 and "price" in d2:
        return float(d2["price"])
    return None


# ============================================================
# MARKET SCANNER
# ============================================================
def scan_markets() -> List[dict]:
    """Scan all crypto up/down markets across timeframes (parallel)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    t0 = now_utc()
    tasks = []
    seen = set()

    for asset in ASSETS:
        for tf in TIMEFRAMES:
            base = t0.replace(second=0, microsecond=0,
                            minute=(t0.minute // tf) * tf)
            for offset in [-tf, 0]:
                ts = int((base + timedelta(minutes=offset)).timestamp())
                slug = f"{asset}-updown-{tf}m-{ts}"
                if slug in seen:
                    continue
                seen.add(slug)
                tasks.append((slug, asset, tf, f"{GAMMA}/markets?slug={slug}"))

    markets = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(curl, url): (slug, asset, tf)
                   for slug, asset, tf, url in tasks}
        for f in as_completed(futures):
            slug, asset, tf = futures[f]
            d = f.result()
            if not d or not isinstance(d, list) or not d:
                continue
            m = d[0]
            if not m.get("active") or m.get("closed"):
                continue
            tokens = json.loads(m.get("clobTokenIds", "[]"))
            prices = json.loads(m.get("outcomePrices", "[]"))
            if len(tokens) < 2:
                continue
            markets.append({
                "question": m.get("question", ""),
                "slug": slug,
                "asset": asset,
                "timeframe": tf,
                "condition_id": m.get("conditionId", ""),
                "token_up": tokens[0],
                "token_down": tokens[1],
                "gamma_up": float(prices[0]) if prices else 0.5,
                "gamma_down": float(prices[1]) if len(prices) >= 2 else 0.5,
                "volume": float(m.get("volume", 0)),
                "end_date": m.get("endDate", ""),
            })
    return markets


# ============================================================
# POSITION TRACKING
# ============================================================
@dataclass
class Position:
    token_id: str
    side: str       # "Up" or "Down"
    entry_price: float
    size: float
    slug: str
    asset: str
    timeframe: int
    entered_at: float
    sell_order_id: str = ""


class OrderManager:
    def __init__(self, client: ClobClient, live: bool = False):
        self.client = client
        self.live = live
        self.positions: Dict[str, Position] = {}  # token_id -> position
        self.open_buy_orders: Dict[str, dict] = {}  # oid -> {token_id, price, size, slug}
        self.open_sell_orders: Dict[str, str] = {}  # oid -> token_id
        self.total_trades = 0
        self.total_pnl = 0.0
        self._state_file = DATA_DIR / "bandit_state.json"
        self._db = init_db()
        self._load()

    def _load(self):
        f = self._state_file
        if f.exists():
            try:
                d = json.loads(f.read_text())
                self.total_trades = d.get("total_trades", 0)
                self.total_pnl = d.get("total_pnl", 0)
                for tid, pdata in d.get("positions", {}).items():
                    pdata["entered_at"] = float(pdata.get("entered_at", 0))
                    self.positions[tid] = Position(**pdata)
            except:
                pass

    def _save(self):
        self._state_file.write_text(json.dumps({
            "total_trades": self.total_trades,
            "total_pnl": self.total_pnl,
            "positions": {k: {
                "token_id": v.token_id, "side": v.side,
                "entry_price": v.entry_price, "size": v.size,
                "slug": v.slug, "asset": v.asset,
                "timeframe": v.timeframe, "entered_at": v.entered_at,
                "sell_order_id": v.sell_order_id,
            } for k, v in self.positions.items()},
            "updated": now_utc().isoformat(),
        }, indent=2, default=str))

    def balance(self) -> float:
        try:
            return float(self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            ).get("balance", 0)) / 1e6
        except:
            return 0

    def _has_position_for(self, token_id: str) -> bool:
        return token_id in self.positions

    def _has_order_for(self, token_id: str) -> bool:
        return any(o.get("token_id") == token_id
                   for o in self.open_buy_orders.values())

    def buy(self, token_id: str, side: str, price: float, size: float,
            slug: str, asset: str, timeframe: int) -> int:
        """Place a limit BUY order. Returns: 1=placed, 0=skipped, -1=dead orderbook."""
        if self._has_position_for(token_id):
            return 0
        if self._has_order_for(token_id):
            return 0
        if len(self.open_buy_orders) + len(self.open_sell_orders) >= MAX_OPEN_ORDERS:
            return 0
        if len(self.positions) >= MAX_POSITIONS:
            return 0
        if price * size < MIN_ORDER_VALUE:
            return 0  # below Polymarket minimum

        price = max(0.01, round(price, 2))

        if not self.live:
            logger.info(f"🔍 DRY BUY: {size} {side} @ ${price:.2f} | {slug[:35]}")
            return 0

        logger.info(f"📝 BUY: {size} {side} @ ${price:.2f} | {slug}")
        try:
            r = self.client.create_and_post_order(OrderArgsV2(
                price=price, size=size, side=BUY, token_id=token_id,
            ), order_type=OrderType.GTC)
        except Exception as e:
            msg = str(e)
            if "does not exist" in msg:
                logger.debug(f"Orderbook not ready for {slug}: skip")
                return -1  # dead orderbook
            elif "minimum" in msg.lower() or "min size" in msg.lower():
                logger.warning(f"Size violation: {msg[:80]}")
            else:
                logger.error(f"Buy failed: {msg[:100]}")
            return 0

        if r:
            oid = str(r.get("orderID", r.get("id", f"b_{time.time()}")))
            self.open_buy_orders[oid] = {
                "token_id": token_id, "side": side, "price": price,
                "size": size, "slug": slug, "asset": asset,
                "timeframe": timeframe, "created_at": time.time(),
            }
            bal = self.balance()
            log_trade(self._db, ts=now_utc().isoformat(), type="buy_order",
                      market=slug, asset=asset, side=side,
                      price=price, size=size,
                      cost=price * size,
                      balance_after=bal,
                      purpose=f"BUY: {side}@{price:.2f} cheap={price<BUY_CHEAP}")
            return 1
        return 0

    def sell(self, pos: Position, price: float) -> bool:
        """Place a limit SELL order for an existing position."""
        if pos.sell_order_id:
            # Already have a sell order, cancel and replace if price changed
            if self.live:
                try:
                    self.client.cancel(pos.sell_order_id)
                except:
                    pass
            if pos.sell_order_id in self.open_sell_orders:
                del self.open_sell_orders[pos.sell_order_id]

        price = min(0.99, max(0.01, round(price, 2)))

        if not self.live:
            logger.info(f"🔍 DRY SELL: {pos.size} {pos.side} @ ${price:.2f} "
                       f"(entry=${pos.entry_price:.2f}, "
                       f"gain=${(price-pos.entry_price)*pos.size:+.2f})")
            return False

        logger.info(f"📤 SELL: {pos.size} {pos.side} @ ${price:.2f} "
                   f"(from ${pos.entry_price:.2f})")
        try:
            r = self.client.create_and_post_order(OrderArgsV2(
                price=price, size=pos.size, side=SELL,
                token_id=pos.token_id,
            ), order_type=OrderType.GTC)
        except Exception as e:
            logger.error(f"Sell failed: {e}")
            return False

        if r:
            oid = str(r.get("orderID", r.get("id", f"s_{time.time()}")))
            self.open_sell_orders[oid] = pos.token_id
            pos.sell_order_id = oid
            return True
        return False

    def check_buy_fills(self):
        """Check if buy orders filled → create positions."""
        filled = []
        for oid, order in list(self.open_buy_orders.items()):
            try:
                status = self.client.get_order(oid)
                if not status:
                    continue
                s = str(status.get("status", "")).lower()
                filled_amt = float(status.get("filled", 0))
                if "filled" in s or "matched" in s or filled_amt > 0:
                    filled.append((oid, order, float(status.get("price_matched", order["price"]))))
            except:
                pass

        for oid, order, fill_price in filled:
            logger.info(f"✅ FILLED BUY: {order['size']} {order['side']} "
                       f"@ ${fill_price:.2f} | {order['slug'][:35]}")
            self.positions[order["token_id"]] = Position(
                token_id=order["token_id"],
                side=order["side"],
                entry_price=fill_price,
                size=order["size"],
                slug=order["slug"],
                asset=order["asset"],
                timeframe=order["timeframe"],
                entered_at=time.time(),
            )
            del self.open_buy_orders[oid]

            bal = self.balance()
            log_trade(self._db, ts=now_utc().isoformat(), type="position_open",
                      market=order["slug"], side=order["side"],
                      asset=order["asset"],
                      price=fill_price, size=order["size"],
                      cost=fill_price * order["size"],
                      balance_after=bal,
                      purpose=f"持有: {order['side']}@{fill_price:.2f} "
                              f"目标 +{int((SELL_TARGET-1)*100)}%")
            self._save()

    def check_sell_fills(self):
        """Check if sell orders filled → close positions, book PnL."""
        filled = []
        for oid, token_id in list(self.open_sell_orders.items()):
            try:
                status = self.client.get_order(oid)
                if not status:
                    continue
                s = str(status.get("status", "")).lower()
                filled_amt = float(status.get("filled", 0))
                if "filled" in s or "matched" in s or filled_amt > 0:
                    filled.append((oid, token_id,
                                  float(status.get("price_matched", 0))))
            except:
                pass

        for oid, token_id, fill_price in filled:
            pos = self.positions.get(token_id)
            if not pos:
                del self.open_sell_orders[oid]
                continue

            pnl = (fill_price - pos.entry_price) * pos.size
            pnl_pct = (fill_price - pos.entry_price) / pos.entry_price * 100

            logger.info(
                f"💰 SOLD: {pos.size} {pos.side} "
                f"entry=${pos.entry_price:.2f} → exit=${fill_price:.2f} "
                f"PnL=${pnl:+.2f} ({pnl_pct:+.1f}%) | {pos.slug[:35]}"
            )

            self.total_trades += 1
            self.total_pnl += pnl
            del self.positions[token_id]
            del self.open_sell_orders[oid]

            bal = self.balance()
            log_trade(self._db, ts=now_utc().isoformat(), type="position_close",
                      market=pos.slug, side=pos.side,
                      asset=pos.asset,
                      price=fill_price, size=pos.size,
                      cost=fill_price * pos.size,
                      profit=pnl,
                      balance_after=bal,
                      purpose=f"卖出: ${pos.entry_price:.2f}→${fill_price:.2f} "
                              f"+${pnl:.2f} ({pnl_pct:+.1f}%)")
            self._save()

    def cancel_stale(self, max_age: float = 120):
        """Cancel stale buy orders."""
        now = time.time()
        for oid, order in list(self.open_buy_orders.items()):
            if now - order["created_at"] > max_age:
                if self.live:
                    try:
                        self.client.cancel(oid)
                    except:
                        pass
                logger.debug(f"🔄 Cancel stale buy: {order['side']} | {order['slug'][:30]}")
                del self.open_buy_orders[oid]

    def active_count(self) -> int:
        return len(self.positions) + len(self.open_buy_orders)


# ============================================================
# BANDIT BOT
# ============================================================
class BanditBot:
    def __init__(self, live=False, size=DEFAULT_SIZE):
        load_dotenv(dotenv_path=BASE_DIR / ".env")
        self.live = live
        self.size = size
        self.running = True
        self._cached_balance = 0.0
        self._last_balance_ts = 0.0

        pk = os.environ.get("POLY_PRIVATE_KEY")
        ak = os.environ.get("POLY_API_KEY", "")
        a_s = os.environ.get("POLY_API_SECRET", "")
        ap = os.environ.get("POLY_API_PASSPHRASE", "")
        prx = os.environ.get("POLY_PROXY_WALLET", "")
        dep = os.environ.get("POLY_DEPOSIT_WALLET", "")
        creds = ApiCreds(api_key=ak, api_secret=a_s, api_passphrase=ap)
        self.clob = ClobClient(
            host=CLOB, chain_id=137, key=pk,
            creds=creds, funder=prx or dep, signature_type=3,
        )

        self.orders = OrderManager(self.clob, live=live)
        self.cycles = 0
        self._last_market_prices: Dict[str, float] = {}  # token_id -> last price
        self._dead_tokens: Set[str] = set()  # token_ids with non-existent orderbooks
        signal.signal(signal.SIGINT, lambda *a: setattr(self, 'running', False))

    def _get_balance(self) -> float:
        """Cached balance, refresh every 3 seconds."""
        now = time.time()
        if now - self._last_balance_ts > 3:
            self._cached_balance = self.orders.balance()
            self._last_balance_ts = now
        return self._cached_balance

    def _should_buy(self, price: float, gamma: float, asset: str) -> bool:
        """Determine if price is attractive to buy."""
        # Skip dead tokens
        if price < MIN_PRICE:
            return False
        # Absolute cheap threshold
        if price < BUY_CHEAP:
            return True
        # Discount from gamma (market sentiment)
        if gamma > 0 and price < gamma - BUY_DISCOUNT:
            return True
        return False

    def _target_sell_price(self, entry: float) -> float:
        """Target sell price based on entry."""
        return min(0.99, round(entry * SELL_TARGET, 2))

    def _stop_price(self, entry: float) -> float:
        """Stop loss price."""
        return max(0.01, round(entry * STOP_LOSS, 2))

    def step(self):
        """One scan cycle: find opportunities, manage positions."""
        markets = scan_markets()
        if not markets:
            return

        # Phase 1: Process fills from previous orders
        if self.live:
            self.orders.check_buy_fills()
            self.orders.check_sell_fills()

        # Phase 2: Evaluate each market for new entries + position management
        for mkt in markets:
            # --- Manage existing positions ---
            if mkt["token_up"] in self.orders.positions:
                pos = self.orders.positions[mkt["token_up"]]
                self._manage_position(pos, mkt["gamma_up"], mkt["token_up"])
            if mkt["token_down"] in self.orders.positions:
                pos = self.orders.positions[mkt["token_down"]]
                self._manage_position(pos, mkt["gamma_down"], mkt["token_down"])

            # --- Look for new buy opportunities ---
            bal = self._get_balance()
            if bal < MIN_BALANCE:
                continue
            if self.orders.active_count() >= MAX_POSITIONS + MAX_OPEN_ORDERS:
                continue

            for side, gamma, token_id in [
                ("Up", mkt["gamma_up"], mkt["token_up"]),
                ("Down", mkt["gamma_down"], mkt["token_down"]),
            ]:
                if token_id in self._dead_tokens:
                    continue
                if self.orders._has_position_for(token_id):
                    continue
                if self.orders._has_order_for(token_id):
                    continue

                # Use gamma as current price estimate
                price = gamma
                if self._should_buy(price, gamma, mkt["asset"]):
                    result = self.orders.buy(
                        token_id=token_id, side=side,
                        price=price, size=self.size,
                        slug=mkt["slug"], asset=mkt["asset"],
                        timeframe=mkt["timeframe"],
                    )
                    if result == -1:
                        self._dead_tokens.add(token_id)

        # Phase 3: Cancel stale orders
        if self.cycles % 10 == 0:
            self.orders.cancel_stale(120)

    def _manage_position(self, pos: Position, current_price: float, token_id: str):
        """Check if position should be sold."""
        if not pos:
            return

        target = self._target_sell_price(pos.entry_price)
        stop = self._stop_price(pos.entry_price)
        held_sec = time.time() - pos.entered_at

        # Time-based exit: auto-sell after MAX_HOLD
        if held_sec > MAX_HOLD:
            logger.info(
                f"⏰ TIME EXIT: {pos.size} {pos.side} "
                f"held {held_sec:.0f}s @ ${pos.entry_price:.2f} cur=${current_price:.2f} "
                f"PnL=${(current_price-pos.entry_price)*pos.size:+.2f} | {pos.slug[:35]}"
            )
            self.orders.sell(pos, max(0.01, current_price))
            return

        # Take profit
        if current_price >= target:
            if not pos.sell_order_id:
                self.orders.sell(pos, current_price)
            return

        # Stop loss
        if current_price <= stop:
            logger.warning(
                f"🛑 STOP LOSS: {pos.size} {pos.side} "
                f"entry=${pos.entry_price:.2f} cur=${current_price:.2f} | {pos.slug[:35]}"
            )
            self.orders.sell(pos, max(0.01, current_price))
            return

        # Adjust sell order if price moved closer to target
        if pos.sell_order_id and current_price > pos.entry_price * 1.05:
            new_target = current_price
            self.orders.sell(pos, new_target)

    def run(self, interval=SCAN_INTERVAL):
        mode = "🚀 LIVE" if self.live else "🔍 DRY RUN"
        bal = self.orders.balance()
        logger.info("=" * 55)
        logger.info(f"🎯 Bandit Bot — {mode}")
        logger.info(f"   Strategy: Buy cheap (<${BUY_CHEAP}), sell +{int((SELL_TARGET-1)*100)}%")
        logger.info(f"   Assets: {', '.join(ASSETS)} | Timeframes: {TIMEFRAMES}")
        logger.info(f"   Size: ${self.size}/trade | Balance: ${bal:.2f}")
        logger.info(f"   Stop loss: -{int((1-STOP_LOSS)*100)}% | Max pos: {MAX_POSITIONS}")
        logger.info("=" * 55)

        logger.info("⏳ Scanning markets...")
        time.sleep(2)

        while self.running:
            try:
                self.cycles += 1
                t0 = time.time()
                self.step()
                elapsed = time.time() - t0

                if self.cycles <= 3 or self.cycles % 5 == 0:
                    bal = self._get_balance()
                    log_balance(self.orders._db, bal,
                                len(self.orders.positions),
                                len(self.orders.open_buy_orders),
                                self.orders.total_trades,
                                self.orders.total_pnl)
                    pos_summary = ", ".join(
                        f"{p.side}@{p.entry_price:.2f}"[:15]
                        for p in list(self.orders.positions.values())[:5]
                    )
                    logger.info(
                        f"📊 C#{self.cycles} ({elapsed:.1f}s) | "
                        f"buys={len(self.orders.open_buy_orders)} "
                        f"holds={len(self.orders.positions)} "
                        f"closed={self.orders.total_trades} "
                        f"PnL=${self.orders.total_pnl:+.2f} | "
                        f"bal=${bal:.2f} | [{pos_summary}]"
                    )

                sleep_t = max(0.5, interval - elapsed)
                time.sleep(sleep_t)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Cycle err: {e}")
                time.sleep(5)

        logger.info(
            f"🏁 Trades: {self.orders.total_trades} "
            f"(${self.orders.total_pnl:+.2f})"
        )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true")
    p.add_argument("--size", type=float, default=DEFAULT_SIZE)
    p.add_argument("--interval", type=float, default=SCAN_INTERVAL)
    a = p.parse_args()
    BanditBot(live=a.live, size=a.size).run(interval=a.interval)


if __name__ == "__main__":
    main()
