"""
⚡ Fast Bandit Bot v2 — Bilateral Pair Arbitrage + Single-Side Sniping

Strategy:
  - SINGLE: Buy cheap tokens (<$0.38), hold for +8% or -25% SL, 90s timeout
  - PAIR:   Buy BOTH UP+DOWN when prices are irrationally low
            Three tiers: aggressive($0.01) / balanced($0.05) / conservative($0.10)
            Order book depth check before placing
            300s order timeout, 120s partial-pair timeout
            Emergency unwind for stranded single-side fills

Usage:
  python3 fast_bot.py --live --size 5
  python3 fast_bot.py                # dry run
"""

import os, sys, json, time, signal, logging, argparse, subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, as_completed

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

GAMMA  = "https://gamma-api.polymarket.com"
CLOB   = "https://clob.polymarket.com"

# ============================================================
# STRATEGY PARAMETERS
# ============================================================

# --- Single-Side Sniping ---
SINGLE_ENABLED = False   # DISABLED — losing money, pair-only mode
BUY_CHEAP     = 0.38
SELL_TARGET   = 1.08   # +8%
STOP_LOSS     = 0.75   # -25%
MIN_PRICE     = 0.08
MAX_HOLD      = 90     # auto-exit after 90s
MAX_POSITIONS = 8
DEFAULT_SIZE  = 5.0

# --- Bilateral Pair Arbitrage ---
# Three-tier limit system (price per side, shares per side, max concurrent)
PAIR_TIERS = [
    {"name": "aggressive", "limit": 0.01, "size": 100, "max_pairs": 1},  # $2 cost → $100 payout
    {"name": "balanced",   "limit": 0.05, "size": 100, "max_pairs": 2},  # $10 cost → $100 payout
    {"name": "conservative","limit": 0.10, "size": 100, "max_pairs": 3}, # $20 cost → $100 payout
]
PAIR_ORDER_TIMEOUT   = 300   # cancel unfilled pair orders after 300s
PAIR_PARTIAL_TIMEOUT = 120   # emergency unwind if only one side fills within 120s
PAIR_MIN_PROFIT      = 0.02  # require at least 2% profit margin (up+down < 0.98)
PAIR_MAX_TOTAL       = 3     # global max concurrent pairs across all tiers

# --- System ---
POLL_MS = 1500
CURL_CMD = ["curl", "-s", "--connect-timeout", "1", "--max-time", "2"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "fast_bot.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("fast_bot")


# ============================================================
# UTILS
# ============================================================
def curl(url: str) -> Optional[dict]:
    try:
        r = subprocess.run(CURL_CMD + [url], capture_output=True, text=True, timeout=4)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except:
        pass
    return None

def curl_raw(url: str) -> Optional[str]:
    try:
        r = subprocess.run(CURL_CMD + [url], capture_output=True, text=True, timeout=4)
        if r.returncode == 0:
            return r.stdout
    except:
        pass
    return None

def now_utc(): return datetime.now(timezone.utc)


# ============================================================
# ORDER BOOK DEPTH CHECKER
# ============================================================
def get_orderbook_depth(token_id: str, side: str = "bids") -> float:
    """
    Get total depth (in shares) at the best price level from CLOB order book.
    side: 'bids' or 'asks'
    Returns total shares available at best price, or 0 if error.
    """
    try:
        url = f"{CLOB}/book?token_id={token_id}"
        data = curl(url)
        if not data:
            return 0
        # CLOB /book returns {"bids": [...], "asks": [...]}
        orders = data.get(side, [])
        if not orders:
            return 0
        # orders is list of {"price": "0.05", "size": "100"}
        total = sum(float(o.get("size", 0)) for o in orders[:5])  # top 5 levels
        return total
    except:
        return 0


# ============================================================
# PRICE FEED
# ============================================================
class PriceFeed:
    """Parallel HTTP price poller for token IDs."""
    
    def __init__(self):
        self.prices: Dict[str, float] = {}
    
    def update(self, token_ids: List[str]):
        urls = {tid: f"{CLOB}/last-trade-price?token_id={tid}" for tid in token_ids}
        with ThreadPoolExecutor(max_workers=12) as ex:
            futures = {ex.submit(curl, url): tid for tid, url in urls.items()}
            for f in as_completed(futures):
                tid = futures[f]
                try:
                    data = f.result()
                    if data and "price" in data:
                        self.prices[tid] = float(data["price"])
                except:
                    pass


# ============================================================
# MARKET DISCOVERY
# ============================================================
def discover_markets() -> List[dict]:
    """Discover active crypto up/down markets."""
    t0 = now_utc()
    tasks = []
    seen = set()
    assets = ["btc", "eth", "xrp", "sol"]
    
    for asset in assets:
        for tf in [5, 15]:
            base = t0.replace(second=0, microsecond=0, minute=(t0.minute // tf) * tf)
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
            if len(tokens) < 2:
                continue
            markets.append({
                "slug": slug, "asset": asset, "timeframe": tf,
                "token_up": tokens[0], "token_down": tokens[1],
            })
    return markets


# ============================================================
# DATA MODELS
# ============================================================
class PairStatus(Enum):
    ORDERING  = "ordering"   # Both limit orders placed, waiting for fills
    PARTIAL   = "partial"    # One side filled, waiting for the other
    COMPLETE  = "complete"   # Both sides filled, holding to settlement
    ABORTED   = "aborted"    # Timed out or cancelled

@dataclass
class Position:
    token_id: str
    side: str
    entry_price: float
    size: float
    slug: str
    asset: str
    entered_at: float
    sell_order_id: str = ""

@dataclass
class PairPosition:
    slug: str
    asset: str
    timeframe: int
    tier: str                           # aggressive/balanced/conservative
    up_token: str
    dn_token: str
    up_size: int
    dn_size: int
    up_price: float
    dn_price: float
    up_order_id: str = ""
    dn_order_id: str = ""
    up_filled: bool = False
    dn_filled: bool = False
    up_fill_price: float = 0.0
    dn_fill_price: float = 0.0
    entered_at: float = 0.0
    status: str = "ordering"


# ============================================================
# ORDER MANAGER
# ============================================================
class OrderManager:
    def __init__(self, client, live=False):
        self.client = client
        self.live = live
        # Single-side positions
        self.positions: Dict[str, Position] = {}
        self.buy_orders: Dict[str, dict] = {}
        self.sell_orders: Dict[str, str] = {}
        # Pair positions
        self.pairs: Dict[str, PairPosition] = {}       # slug -> PairPosition
        self.pair_orders: Dict[str, str] = {}           # oid -> slug
        # Stats
        self.dead_tokens = set()
        self.total_closed = 0
        self.total_pnl = 0.0
        self.pair_closed = 0
        self.pair_pnl = 0.0
        self.pair_aborted = 0
        self._state_file = DATA_DIR / "fast_state.json"
        self._db = init_db()
        self._load()
    
    def _load(self):
        f = self._state_file
        if f.exists():
            try:
                d = json.loads(f.read_text())
                self.total_closed = d.get("total_closed", 0)
                self.total_pnl = d.get("total_pnl", 0)
                self.pair_closed = d.get("pair_closed", 0)
                self.pair_pnl = d.get("pair_pnl", 0)
                self.pair_aborted = d.get("pair_aborted", 0)
                self.dead_tokens = set(d.get("dead_tokens", []))
                for tid, p in d.get("positions", {}).items():
                    p["entered_at"] = float(p.get("entered_at", 0))
                    self.positions[tid] = Position(**p)
            except:
                pass
    
    def _save(self):
        data = {
            "total_closed": self.total_closed,
            "total_pnl": self.total_pnl,
            "pair_closed": self.pair_closed,
            "pair_pnl": self.pair_pnl,
            "pair_aborted": self.pair_aborted,
            "dead_tokens": list(self.dead_tokens),
            "positions": {k: {
                "token_id": v.token_id, "side": v.side,
                "entry_price": v.entry_price, "size": v.size,
                "slug": v.slug, "asset": v.asset,
                "entered_at": v.entered_at, "sell_order_id": v.sell_order_id,
            } for k, v in self.positions.items()},
            # Persist active pairs
            "pairs": {k: {
                "slug": v.slug, "asset": v.asset, "timeframe": v.timeframe,
                "tier": v.tier, "up_token": v.up_token, "dn_token": v.dn_token,
                "up_size": v.up_size, "dn_size": v.dn_size,
                "up_price": v.up_price, "dn_price": v.dn_price,
                "up_order_id": v.up_order_id, "dn_order_id": v.dn_order_id,
                "up_filled": v.up_filled, "dn_filled": v.dn_filled,
                "up_fill_price": v.up_fill_price, "dn_fill_price": v.dn_fill_price,
                "entered_at": v.entered_at, "status": v.status,
            } for k, v in self.pairs.items()},
            "updated": now_utc().isoformat(),
        }
        self._state_file.write_text(json.dumps(data, indent=2, default=str))
    
    def balance(self) -> float:
        try:
            return float(self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            ).get("balance", 0)) / 1e6
        except:
            return 0
    
    # ================================================================
    # PAIR OPERATIONS
    # ================================================================
    
    def _can_pair(self, slug: str, tier: dict) -> Tuple[bool, str]:
        """Check if a pair can be placed."""
        if slug in self.pairs:
            return False, "already paired"
        
        # Count pairs per tier
        tier_count = sum(1 for p in self.pairs.values() if p.tier == tier["name"])
        if tier_count >= tier["max_pairs"]:
            return False, f"tier {tier['name']} full ({tier_count}/{tier['max_pairs']})"
        
        total_pairs = len(self.pairs)
        if total_pairs >= PAIR_MAX_TOTAL:
            return False, f"global pair limit reached ({total_pairs}/{PAIR_MAX_TOTAL})"
        
        return True, ""
    
    def buy_pair(self, market: dict, tier: dict, up_price: float, down_price: float) -> bool:
        """
        Place bilateral limit orders with depth check.
        Returns True if both orders were placed successfully.
        """
        slug = market["slug"]
        can, reason = self._can_pair(slug, tier)
        if not can:
            return False
        
        up_token = market["token_up"]
        dn_token = market["token_down"]
        size = tier["size"]
        
        if up_token in self.dead_tokens or dn_token in self.dead_tokens:
            return False
        
        # Check order book depth
        up_depth = get_orderbook_depth(up_token, "bids")
        dn_depth = get_orderbook_depth(dn_token, "bids")
        min_depth = size * 2  # need at least 2x our size for reasonable fill chance
        
        if up_depth < min_depth:
            logger.debug(f"  UP depth insufficient: {up_depth:.0f} < {min_depth}")
        if dn_depth < min_depth:
            logger.debug(f"  DOWN depth insufficient: {dn_depth:.0f} < {min_depth}")
        
        total_cost = (up_price + down_price) * size
        pair_payout = size * 1.0
        pair_roi = (pair_payout - total_cost) / total_cost * 100
        
        if not self.live:
            logger.info(
                f"🔷 DRY PAIR [{tier['name']}]: {size}×2 {slug[:35]} | "
                f"UP${up_price:.3f}+DN${down_price:.3f} | "
                f"cost=${total_cost:.2f}→${pair_payout:.2f} | "
                f"ROI={pair_roi:.0f}% | depth UP={up_depth:.0f}/DN={dn_depth:.0f}"
            )
            return False
        
        logger.info(
            f"🔷 PAIR [{tier['name']}]: {size}×2 {slug[:35]} | "
            f"UP${up_price:.4f}+DN${down_price:.4f} | "
            f"depth UP={up_depth:.0f}/DN={dn_depth:.0f}"
        )
        
        pair = PairPosition(
            slug=slug, asset=market["asset"], timeframe=market["timeframe"],
            tier=tier["name"],
            up_token=up_token, dn_token=dn_token,
            up_size=size, dn_size=size,
            up_price=up_price, dn_price=down_price,
            entered_at=time.time(), status="ordering",
        )
        
        success_up = False
        success_dn = False
        
        # Place UP order
        try:
            r = self.client.create_and_post_order(OrderArgsV2(
                price=up_price, size=size, side=BUY, token_id=up_token,
            ), order_type=OrderType.GTC)
            if r:
                oid = str(r.get("orderID", r.get("id", f"pu_{time.time()}")))
                pair.up_order_id = oid
                self.pair_orders[oid] = slug
                success_up = True
                logger.info(f"  ✅ UP: {size} @ ${up_price:.4f} | oid={oid[:24]}")
        except Exception as e:
            msg = str(e)
            if "does not exist" in msg:
                self.dead_tokens.add(up_token)
            logger.warning(f"  ❌ UP fail: {msg[:80]}")
        
        # Place DOWN order
        try:
            r = self.client.create_and_post_order(OrderArgsV2(
                price=down_price, size=size, side=BUY, token_id=dn_token,
            ), order_type=OrderType.GTC)
            if r:
                oid = str(r.get("orderID", r.get("id", f"pd_{time.time()}")))
                pair.dn_order_id = oid
                self.pair_orders[oid] = slug
                success_dn = True
                logger.info(f"  ✅ DOWN: {size} @ ${down_price:.4f} | oid={oid[:24]}")
        except Exception as e:
            msg = str(e)
            if "does not exist" in msg:
                self.dead_tokens.add(dn_token)
            logger.warning(f"  ❌ DOWN fail: {msg[:80]}")
        
        if success_up and success_dn:
            self.pairs[slug] = pair
            self._save()
            logger.info(f"  🎯 PAIR ACTIVE: cost=${total_cost:.2f} → ${pair_payout:.2f} (+{pair_roi:.0f}%)")
            return True
        elif success_up or success_dn:
            # Cancel the one that went through
            oid_to_cancel = pair.up_order_id if success_up else pair.dn_order_id
            if oid_to_cancel:
                try:
                    self.client.cancel(oid_to_cancel)
                except:
                    pass
                if oid_to_cancel in self.pair_orders:
                    del self.pair_orders[oid_to_cancel]
            logger.warning(f"  ⚠️ Partial placement: only {success_up+success_dn}/2, abandoning")
        
        return False
    
    def check_pair_fills(self):
        """Check pair order fills and manage pair lifecycle."""
        now = time.time()
        oids_to_remove = []
        
        for oid, slug in list(self.pair_orders.items()):
            pair = self.pairs.get(slug)
            if not pair:
                oids_to_remove.append(oid)
                continue
            
            try:
                s = self.client.get_order(oid)
                if not s:
                    continue
                status = str(s.get("status", "")).lower()
                
                if status in ("filled", "matched"):
                    # Get fill price
                    price = (float(s.get("price_matched", 0)) or
                            float(s.get("avg_price", 0)) or
                            float(s.get("price", 0)))
                    
                    if oid == pair.up_order_id:
                        pair.up_filled = True
                        pair.up_fill_price = price if price > 0.001 else pair.up_price
                        logger.info(f"  ✅ PAIR UP FILLED: ${pair.up_fill_price:.4f} | {slug[:35]}")
                    elif oid == pair.dn_order_id:
                        pair.dn_filled = True
                        pair.dn_fill_price = price if price > 0.001 else pair.dn_price
                        logger.info(f"  ✅ PAIR DOWN FILLED: ${pair.dn_fill_price:.4f} | {slug[:35]}")
                    
                    oids_to_remove.append(oid)
                    
                    # Check pair completion
                    if pair.up_filled and pair.dn_filled:
                        self._complete_pair(pair)
                    elif pair.up_filled or pair.dn_filled:
                        pair.status = "partial"
                        logger.info(f"  ⏳ PAIR PARTIAL [{pair.tier}]: waiting for other side | {slug[:35]}")
                
                elif status in ("cancelled", "expired"):
                    oids_to_remove.append(oid)
                    if not pair.up_filled and not pair.dn_filled:
                        # Both cancelled → abort
                        self._abort_pair(pair, "orders cancelled")
                    
            except:
                pass
        
        for oid in oids_to_remove:
            if oid in self.pair_orders:
                del self.pair_orders[oid]
    
    def cancel_stale_pair_orders(self):
        """Cancel pair orders that have been open too long without filling."""
        now = time.time()
        for slug, pair in list(self.pairs.items()):
            if pair.status == "complete":
                continue
            
            age = now - pair.entered_at
            
            # Check for timeout on unfilled orders
            if not pair.up_filled and not pair.dn_filled and age > PAIR_ORDER_TIMEOUT:
                # Cancel both
                for oid in [pair.up_order_id, pair.dn_order_id]:
                    if oid:
                        try:
                            self.client.cancel(oid)
                        except:
                            pass
                self._abort_pair(pair, f"order timeout ({age:.0f}s)")
                continue
            
            # Check for partial fill timeout
            if pair.status == "partial" and age > PAIR_PARTIAL_TIMEOUT:
                self._emergency_unwind(pair)
                continue
    
    def cancel_already_held_market_orders(self, market: dict):
        """Cancel pair orders for tokens we now hold via single-side buys."""
        up_tok = market["token_up"]
        dn_tok = market["token_down"]
        if up_tok in self.positions or dn_tok in self.positions:
            for slug, pair in list(self.pairs.items()):
                if pair.up_token == up_tok or pair.dn_token == dn_tok:
                    for oid in [pair.up_order_id, pair.dn_order_id]:
                        if oid and oid in self.pair_orders:
                            try:
                                self.client.cancel(oid)
                            except:
                                pass
                    self._abort_pair(pair, "token held via single-side buy")
    
    def _complete_pair(self, pair: PairPosition):
        """Both sides filled. Record profit."""
        cost = (pair.up_fill_price * pair.up_size + pair.dn_fill_price * pair.dn_size)
        payout = pair.up_size * 1.0
        profit = payout - cost
        
        pair.status = "complete"
        self.pair_closed += 1
        self.pair_pnl += profit
        self.total_closed += 1
        self.total_pnl += profit
        
        roi = profit / cost * 100 if cost > 0 else 0
        logger.info(
            f"🔷🔷 PAIR COMPLETE [{pair.tier}]: {pair.slug[:35]} | "
            f"UP${pair.up_fill_price:.4f}+DN${pair.dn_fill_price:.4f} | "
            f"cost=${cost:.2f} → ${payout:.2f} | +${profit:.2f} ({roi:.0f}%)"
        )
        
        # Clean up
        if pair.slug in self.pairs:
            del self.pairs[pair.slug]
        self._save()
    
    def _abort_pair(self, pair: PairPosition, reason: str):
        """Abort a pair that couldn't complete."""
        self.pair_aborted += 1
        cost_estimate = (pair.up_price * pair.up_size + pair.dn_price * pair.dn_size)
        logger.warning(
            f"❌ PAIR ABORTED [{pair.tier}]: {pair.slug[:35]} | "
            f"reason={reason} | est_cost=${cost_estimate:.2f}"
        )
        # Clean up order tracking
        for oid in list(self.pair_orders.keys()):
            if self.pair_orders[oid] == pair.slug:
                del self.pair_orders[oid]
        if pair.slug in self.pairs:
            del self.pairs[pair.slug]
        self._save()
    
    def _emergency_unwind(self, pair: PairPosition):
        """Partial pair timed out. Market-sell the filled side to limit loss."""
        logger.warning(f"🚨 EMERGENCY UNWIND [{pair.tier}]: {pair.slug[:35]}")
        
        # Cancel unfilled order
        unfilled_oid = pair.dn_order_id if pair.up_filled else pair.up_order_id
        if unfilled_oid:
            try:
                self.client.cancel(unfilled_oid)
            except:
                pass
        
        # Market-sell the filled side
        if pair.up_filled and not pair.dn_filled:
            token_id = pair.up_token
            size = pair.up_size
            side = "UP"
        elif pair.dn_filled and not pair.up_filled:
            token_id = pair.dn_token
            size = pair.dn_size
            side = "DOWN"
        else:
            self._abort_pair(pair, "emergency unwind - unknown state")
            return
        
        # Sell at market (price=0.01 for fast fill)
        try:
            r = self.client.create_and_post_order(OrderArgsV2(
                price=0.01, size=size, side=SELL, token_id=token_id,
            ), order_type=OrderType.GTC)
            if r:
                oid = str(r.get("orderID", r.get("id", "")))
                logger.info(f"  🆘 EMERGENCY SELL: {size} {side} MARKET | oid={oid[:24]}")
                # Track as sell order
                self.sell_orders[oid] = token_id
        except Exception as e:
            logger.error(f"  💥 Emergency sell failed: {str(e)[:80]}")
        
        loss = pair.up_price * pair.up_size if pair.up_filled else pair.dn_price * pair.dn_size
        self.pair_pnl -= loss
        self.total_pnl -= loss
        self._abort_pair(pair, f"partial unwind - lost ~${loss:.2f}")
    
    # ================================================================
    # SINGLE-SIDE OPERATIONS (unchanged from v1)
    # ================================================================
    
    def buy(self, token_id, side, price, size, slug, asset) -> bool:
        bought_tokens = {info["token_id"] for info in self.buy_orders.values()}
        if token_id in self.positions or token_id in bought_tokens:
            return False
        if token_id in self.dead_tokens:
            return False
        if len(self.positions) >= MAX_POSITIONS:
            return False
        price = max(0.01, round(price, 2))
        if price * size < 1.0:
            return False
        if not self.live:
            logger.info(f"DRY BUY: {size} {side} @ ${price:.2f}")
            return False
        logger.info(f"📝 BUY {size} {side} @ ${price:.2f} | {slug[:30]}")
        try:
            r = self.client.create_and_post_order(OrderArgsV2(
                price=price, size=size, side=BUY, token_id=token_id,
            ), order_type=OrderType.GTC)
            if r:
                oid = str(r.get("orderID", r.get("id", f"b_{time.time()}")))
                self.buy_orders[oid] = {
                    "token_id": token_id, "side": side,
                    "slug": slug, "asset": asset, "size": size,
                }
                return True
        except Exception as e:
            msg = str(e)
            if "does not exist" in msg:
                self.dead_tokens.add(token_id)
            logger.debug(f"Buy err: {msg[:80]}")
        return False
    
    def sell(self, pos, price) -> bool:
        if pos.sell_order_id:
            try:
                self.client.cancel(pos.sell_order_id)
            except:
                pass
            if pos.sell_order_id in self.sell_orders:
                del self.sell_orders[pos.sell_order_id]
        price = min(0.99, max(0.01, round(price, 2)))
        if not self.live:
            logger.info(f"DRY SELL: {pos.size} {pos.side} @ ${price:.2f} (entry=${pos.entry_price:.2f})")
            return False
        logger.info(f"📤 SELL {pos.size} {pos.side} @ ${price:.2f} | {pos.slug[:30]}")
        try:
            r = self.client.create_and_post_order(OrderArgsV2(
                price=price, size=pos.size, side=SELL, token_id=pos.token_id,
            ), order_type=OrderType.GTC)
            if r:
                oid = str(r.get("orderID", r.get("id", f"s_{time.time()}")))
                self.sell_orders[oid] = pos.token_id
                pos.sell_order_id = oid
                return True
        except Exception as e:
            logger.debug(f"Sell err: {str(e)[:80]}")
        return False
    
    def check_fills(self):
        """Check single-side buy and sell order fills."""
        # Check buy fills
        for oid, info in list(self.buy_orders.items()):
            try:
                s = self.client.get_order(oid)
                if not s: continue
                status = str(s.get("status", "")).lower()
                if status in ("filled", "matched"):
                    price = (float(s.get("price_matched", 0)) or
                            float(s.get("avg_price", 0)) or
                            float(s.get("price", 0)))
                    if price <= 0.001: continue
                    self.positions[info["token_id"]] = Position(
                        token_id=info["token_id"], side=info["side"],
                        entry_price=price, size=info["size"],
                        slug=info["slug"], asset=info["asset"],
                        entered_at=time.time(),
                    )
                    del self.buy_orders[oid]
                    logger.info(f"✅ FILLED BUY: {info['side']} @ ${price:.2f} | {info['slug'][:30]}")
                    self._save()
            except: pass
        
        # Check sell fills
        for oid, token_id in list(self.sell_orders.items()):
            try:
                s = self.client.get_order(oid)
                if not s: continue
                status = str(s.get("status", "")).lower()
                if status in ("filled", "matched"):
                    pos = self.positions.get(token_id)
                    if pos:
                        exit_price = (float(s.get("price_matched", 0)) or
                                     float(s.get("avg_price", 0)) or
                                     float(s.get("price", 0)))
                        if exit_price <= 0.001:
                            exit_price = pos.entry_price  # fallback
                        pnl = (exit_price - pos.entry_price) * pos.size
                        logger.info(f"💰 SOLD: {pos.side} ${pos.entry_price:.2f}→${exit_price:.2f} PnL=${pnl:+.2f}")
                        self.total_closed += 1
                        self.total_pnl += pnl
                        del self.positions[token_id]
                    del self.sell_orders[oid]
                    self._save()
            except: pass


# ============================================================
# FAST BOT
# ============================================================
class FastBanditBot:
    def __init__(self, live=False, size=DEFAULT_SIZE):
        load_dotenv(dotenv_path=BASE_DIR / ".env")
        self.live = live
        self.size = size
        self.running = True
        
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
        self.feed = PriceFeed()
        self.markets: List[dict] = []
        self.cycles = 0
        self._cached_balance = 0.0
        self._last_bal_ts = 0.0
        signal.signal(signal.SIGINT, lambda *a: setattr(self, 'running', False))
    
    def _get_balance(self):
        now = time.time()
        if now - self._last_bal_ts > 5:
            self._cached_balance = self.orders.balance()
            self._last_bal_ts = now
        return self._cached_balance
    
    def _get_all_token_ids(self) -> List[str]:
        ids = set()
        for m in self.markets:
            ids.add(m["token_up"])
            ids.add(m["token_down"])
        ids.update(self.orders.positions.keys())
        ids.update(info["token_id"] for info in self.orders.buy_orders.values())
        # Add pair tokens
        for pair in self.orders.pairs.values():
            ids.add(pair.up_token)
            ids.add(pair.dn_token)
        return list(ids)
    
    def refresh_markets(self):
        markets = discover_markets()
        if markets:
            self.markets = markets
            logger.info(f"Markets refreshed: {len(markets)} active")
    
    def step(self):
        """Main loop iteration."""
        # 1. Get fresh prices
        token_ids = self._get_all_token_ids()
        if token_ids:
            self.feed.update(token_ids)
        
        # 2. Check order fills
        if self.live:
            self.orders.check_fills()
            self.orders.check_pair_fills()
            self.orders.cancel_stale_pair_orders()
        
        # 3. Manage single-side positions (DISABLED)
        if SINGLE_ENABLED:
            for token_id, pos in list(self.orders.positions.items()):
                price = self.feed.prices.get(token_id)
                if price is None:
                    continue
                
                held = time.time() - pos.entered_at
                if held > MAX_HOLD:
                    if not pos.sell_order_id:
                        logger.info(f"⏰ EXIT: {pos.side} held {held:.0f}s cur=${price:.2f}")
                        self.orders.sell(pos, price)
                    continue
                if not pos.sell_order_id and price >= pos.entry_price * SELL_TARGET:
                    logger.info(f"🎯 TP: {pos.side} ${pos.entry_price:.2f}→${price:.2f}")
                    self.orders.sell(pos, price)
                    continue
                if not pos.sell_order_id and price <= pos.entry_price * STOP_LOSS:
                    logger.warning(f"🛑 SL: {pos.side} ${pos.entry_price:.2f}→${price:.2f}")
                    self.orders.sell(pos, price)
                    continue
        
        # 4. Single-side buy opportunities (DISABLED)
        if SINGLE_ENABLED:
            for m in self.markets:
                if len(self.orders.positions) >= MAX_POSITIONS:
                    break
                for side, token_id in [("Up", m["token_up"]), ("Down", m["token_down"])]:
                    if token_id in self.orders.positions:
                        continue
                    bought = {info["token_id"] for info in self.orders.buy_orders.values()}
                    if token_id in bought:
                        continue
                    price = self.feed.prices.get(token_id)
                    if price is None or price < MIN_PRICE:
                        continue
                    if price < BUY_CHEAP:
                        self.orders.buy(token_id, side, price, self.size, m["slug"], m["asset"])
        
        # 5. PAIR SCANNER — Check all tiers for bilateral opportunities
        for m in self.markets:
            up_price = self.feed.prices.get(m["token_up"])
            dn_price = self.feed.prices.get(m["token_down"])
            if up_price is None or dn_price is None:
                continue
            
            pair_cost = up_price + dn_price
            
            # Skip if already paired or not enough profit margin
            if m["slug"] in self.orders.pairs:
                continue
            if pair_cost >= (1.0 - PAIR_MIN_PROFIT):
                continue
            
            # Check each tier from most profitable to least
            for tier in PAIR_TIERS:
                if up_price <= tier["limit"] and dn_price <= tier["limit"]:
                    # Cancel any conflicting single-side orders for this market
                    self.orders.cancel_already_held_market_orders(m)
                    self.orders.buy_pair(m, tier, up_price, dn_price)
                    break  # one tier per market per cycle
    
    def run(self):
        mode = "🚀 LIVE" if self.live else "🔍 DRY RUN"
        single_status = "OFF" if not SINGLE_ENABLED else f"Buy<${BUY_CHEAP} | TP+{int((SELL_TARGET-1)*100)}% | SL-{int((1-STOP_LOSS)*100)}% | {MAX_HOLD}s"
        logger.info("=" * 55)
        logger.info(f"⚡ Fast Bandit Bot v2 — {mode}")
        logger.info(f"   Single: {single_status}")
        logger.info(f"   Pairs:")
        for t in PAIR_TIERS:
            logger.info(f"     [{t['name']}]: ≤${t['limit']:.2f} × {t['size']}sh | max {t['max_pairs']} pairs")
        logger.info(f"   Pair timeout: {PAIR_ORDER_TIMEOUT}s order / {PAIR_PARTIAL_TIMEOUT}s partial")
        logger.info("=" * 55)
        
        logger.info("Discovering markets...")
        self.refresh_markets()
        
        while self.running:
            try:
                self.cycles += 1
                t0 = time.time()
                
                self.step()
                
                elapsed = time.time() - t0
                
                # Refresh markets periodically
                if self.cycles % 30 == 0:
                    self.refresh_markets()
                
                # Status log
                if self.cycles % 5 == 0:
                    bal = self._get_balance()
                    pair_summary = "/".join(
                        f"{t['name'][:3]}:{sum(1 for p in self.orders.pairs.values() if p.tier==t['name'])}"
                        for t in PAIR_TIERS
                    )
                    logger.info(
                        f"📊 C#{self.cycles} | "
                        f"holds={len(self.orders.positions)} "
                        f"pairs={len(self.orders.pairs)}({pair_summary}) "
                        f"cls={self.orders.total_closed} "
                        f"PnL=${self.orders.total_pnl:+.2f}(pair=${self.orders.pair_pnl:+.2f}) | "
                        f"bal=${bal:.2f} | "
                        f"({elapsed*1000:.0f}ms)"
                    )
                
                sleep_ms = max(100, POLL_MS - elapsed * 1000) / 1000
                time.sleep(sleep_ms)
                
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Cycle err: {e}")
                time.sleep(2)
        
        logger.info(f"🏁 Closed: {self.orders.total_closed} (${self.orders.total_pnl:+.2f})")
        logger.info(f"   Pairs: {self.orders.pair_closed} complete, {self.orders.pair_aborted} aborted (${self.orders.pair_pnl:+.2f})")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true")
    p.add_argument("--size", type=float, default=DEFAULT_SIZE)
    a = p.parse_args()
    FastBanditBot(live=a.live, size=a.size).run()


if __name__ == "__main__":
    main()
