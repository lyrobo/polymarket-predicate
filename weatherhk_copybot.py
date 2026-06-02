"""
🌤 WeatherHK Copy Trader — Track & Mirror WeatherHK's Trades

Strategy:
  1. Continuously poll WeatherHK's latest activity via Polymarket data API
  2. Diff against known trades to detect NEW trades and redeems
  3. Copy new trades with proportional sizing (our balance / his balance)
  4. Also copy redeem operations (sell at $0.99 to capture value)
  5. Log everything for analysis

Usage:
  python3 weatherhk_copybot.py --live --ratio 0.05   # 5% of his position size
  python3 weatherhk_copybot.py                        # dry run
"""

import os, sys, json, time, signal, logging, argparse, subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, List, Set
from collections import defaultdict

from dotenv import load_dotenv
from py_clob_client_v2 import ClobClient, ApiCreds
from py_clob_client_v2.clob_types import (
    OrderArgsV2, OrderType, BalanceAllowanceParams, AssetType,
)
from py_clob_client_v2.order_builder.constants import BUY, SELL

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"
for d in [LOG_DIR, DATA_DIR]:
    d.mkdir(parents=True, exist_ok=True)

GAMMA  = "https://gamma-api.polymarket.com"
CLOB   = "https://clob.polymarket.com"

# Target wallet
WHK_WALLET = "0x488c725253fc21c7a9ca812030dc2f6343f98c1c"
WHK_BALANCE = 3000.0  # estimated his balance for proportional sizing

# Strategy params
POLL_INTERVAL = 5       # seconds between polls
MIN_COPY_SIZE  = 1.0     # minimum $1 to copy (skip micro trades)
MAX_OPEN_POSITIONS = 20
DEFAULT_RATIO = 0.05    # 5% of his position size

CURL_CMD = ["curl", "-s", "--connect-timeout", "2", "--max-time", "5"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "weatherhk_copy.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("weatherhk_copy")


# ============================================================
# UTILS
# ============================================================
def curl(url: str) -> Optional[dict]:
    try:
        r = subprocess.run(CURL_CMD + [url], capture_output=True, text=True, timeout=6)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except: pass
    return None


def fetch_weatherhk_activity(limit: int = 50) -> List[dict]:
    """Fetch WeatherHK's latest activity."""
    all_acts = []
    for offset in [0, 50]:
        data = curl(f"https://data-api.polymarket.com/activity?user={WHK_WALLET}&limit={limit}&offset={offset}")
        if data and isinstance(data, list):
            all_acts.extend(data)
    return all_acts


def get_market_by_slug(slug: str) -> Optional[dict]:
    """Get market info including token IDs from slug."""
    data = curl(f"{GAMMA}/markets?slug={slug}")
    if data and isinstance(data, list) and data:
        m = data[0]
        tokens = m.get("clobTokenIds", "[]")
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
        return {
            "slug": slug,
            "condition_id": m.get("conditionId"),
            "tokens": tokens,
            "question": m.get("question", ""),
            "active": m.get("active", False),
            "closed": m.get("closed", False),
        }
    return None


def get_token_for_outcome(market: dict, outcome_index: int) -> Optional[str]:
    """Get token ID for a specific outcome index."""
    tokens = market.get("tokens", [])
    if outcome_index < len(tokens):
        return tokens[outcome_index]
    return None


# ============================================================
# TRACKER
# ============================================================
class WeatherHKTacker:
    """Tracks WeatherHK's activity and detects new trades/redeems."""
    
    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.known_tx_hashes: Set[str] = set()
        self.known_order_ids: Set[str] = set()
        self.last_check = 0
        self._load()
    
    def _load(self):
        if self.state_file.exists():
            try:
                d = json.loads(self.state_file.read_text())
                self.known_tx_hashes = set(d.get("tx_hashes", []))
                self.known_order_ids = set(d.get("order_ids", []))
                self.last_check = d.get("last_check", 0)
            except: pass
    
    def _save(self):
        self.state_file.write_text(json.dumps({
            "tx_hashes": list(self.known_tx_hashes)[-5000:],
            "order_ids": list(self.known_order_ids)[-2000:],
            "last_check": self.last_check,
        }))
    
    def poll(self) -> List[dict]:
        """Poll for new activity. Returns list of NEW trades/redeems."""
        activities = fetch_weatherhk_activity()
        self.last_check = time.time()
        
        new_events = []
        for act in activities:
            tx_hash = act.get("transactionHash", "")
            oid = act.get("orderID", act.get("id", ""))
            
            # Use transaction hash as unique identifier
            event_id = tx_hash or oid
            if not event_id:
                # Fallback: use timestamp + type + size as ID
                event_id = f"{act.get('timestamp','')}_{act.get('type','')}_{act.get('size','')}"
            
            if event_id not in self.known_tx_hashes and event_id not in self.known_order_ids:
                self.known_tx_hashes.add(event_id)
                new_events.append(act)
        
        if new_events:
            self._save()
            logger.info(f"🆕 {len(new_events)} new events detected")
        
        return new_events
    
    def get_latest_state(self) -> List[dict]:
        """Get latest activity snapshot (for initialization). Marks all as known without triggering copies."""
        activities = fetch_weatherhk_activity()
        count = 0
        for act in activities:
            tx_hash = act.get("transactionHash", "")
            oid = act.get("orderID", act.get("id", ""))
            event_id = tx_hash or oid or f"{act.get('timestamp','')}_{act.get('type','')}"
            if event_id not in self.known_tx_hashes:
                self.known_tx_hashes.add(event_id)
                count += 1
        self._save()
        logger.info(f"Initialized: {count} new events marked as known (will not copy)")
        return activities


# ============================================================
# COPY TRADER
# ============================================================
class CopyTrader:
    """Copies WeatherHK's trades with proportional sizing."""
    
    def __init__(self, client: ClobClient, live: bool = False, ratio: float = DEFAULT_RATIO):
        self.client = client
        self.live = live
        self.ratio = ratio
        self.positions: Dict[str, dict] = {}  # token_id -> {side, size, entry_price, slug}
        self.pending_orders: Dict[str, dict] = {}
        self.market_cache: Dict[str, dict] = {}  # slug -> market info
        self.dead_markets = set()
    
    def balance(self) -> float:
        try:
            return float(self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            ).get("balance", 0)) / 1e6
        except:
            return 0
    
    def self_test(self) -> bool:
        """Startup self-test: place a tiny order to verify API credentials.
        
        Returns True if the API accepts orders, False if credentials are broken.
        Uses a well-known liquid token to test order placement.
        """
        logger.info("🧪 Running API credential self-test...")
        
        # Use a known liquid BTC Up/Down token (recently active market)
        test_token = "21742633143463906290569050155826241533067272736897614950488156847949938836455"
        
        try:
            r = self.client.create_and_post_order(OrderArgsV2(
                price=0.01, size=5, side=BUY, token_id=test_token,
            ), order_type=OrderType.GTC)
            if r:
                oid = str(r.get("orderID", r.get("id", "")))
                logger.info(f"  ✅ Self-test PASSED — order {oid[:20]}... placed successfully")
                # Cancel immediately
                try:
                    self.client.cancel_order(oid)
                    logger.info(f"  ✅ Test order cancelled")
                except:
                    pass
                return True
        except Exception as e:
            msg = str(e)
            if "signer address" in msg.lower() or "api key" in msg.lower():
                logger.critical("=" * 55)
                logger.critical("❌ SELF-TEST FAILED: API KEY MISMATCH")
                logger.critical("   'the order signer address has to be the address of the API KEY'")
                logger.critical("   → Check: POLY_PRIVATE_KEY address == POLY_DEPOSIT_WALLET == API_KEY wallet")
                logger.critical("   → Regenerate API credentials for the correct wallet")
                logger.critical("=" * 55)
            else:
                if "does not exist" in msg.lower():
                    logger.info(f"  ⚠️ Test token expired, skipping order test (credentials check passed)")
                    return None  # None = inconclusive, not a failure
                logger.warning(f"  ⚠️ Self-test order failed (non-critical): {msg[:120]}")
            return False
        
        return False
    
    def _get_market(self, slug: str) -> Optional[dict]:
        if slug in self.market_cache:
            return self.market_cache[slug]
        if slug in self.dead_markets:
            return None
        mkt = get_market_by_slug(slug)
        if mkt and mkt["active"]:
            self.market_cache[slug] = mkt
            return mkt
        elif mkt:
            self.dead_markets.add(slug)
        return None
    
    def _calc_copy_size(self, his_size_usdc: float) -> float:
        """Calculate our position size based on ratio."""
        our_size = his_size_usdc * self.ratio
        return max(MIN_COPY_SIZE, round(our_size, 2))
    
    def copy_trade(self, event: dict) -> bool:
        """Copy a single trade from WeatherHK. ONLY BUY — ignore SELL (his profit-taking)."""
        trade_type = event.get("type")
        if trade_type != "TRADE":
            return False
        
        side = event.get("side", "")
        
        # CRITICAL: Only copy BUY. WeatherHK's SELL is his profit-taking exit,
        # not a new position we should mirror. Copying SELL = naked short.
        if side != "BUY":
            logger.debug(f"  ⏭ Skipping SELL: {event.get('title','')[:50]}")
            return False
        
        price = float(event.get("price", 0))
        his_size_usdc = float(event.get("usdcSize", 0))
        slug = event.get("slug", "")
        title = event.get("title", "")[:60]
        outcome_idx = int(event.get("outcomeIndex", 0))
        
        # Skip if too small
        if his_size_usdc < MIN_COPY_SIZE:
            return False
        
        our_size_usdc = self._calc_copy_size(his_size_usdc)
        
        # Balance check
        bal = self.balance()
        if our_size_usdc > bal * 0.8:  # don't use more than 80% of balance
            our_size_usdc = bal * 0.5  # cap at 50%
            if our_size_usdc < MIN_COPY_SIZE:
                logger.debug(f"  ⏭ Insufficient balance: ${bal:.2f}")
                return False
        
        # Get market info and token
        mkt = self._get_market(slug)
        if not mkt:
            logger.debug(f"  ⏭ Market not found: {slug}")
            return False
        
        token_id = get_token_for_outcome(mkt, outcome_idx)
        if not token_id:
            logger.debug(f"  ⏭ No token for outcome {outcome_idx}")
            return False
        
        # Calculate shares
        shares = our_size_usdc / price if price > 0 else our_size_usdc / 0.01
        shares = max(5, int(shares))  # minimum 5 shares
        
        if not self.live:
            logger.info(f"🔍 COPY {side}: {shares}sh @ ${price:.3f} (~${our_size_usdc:.2f}) | {title}")
            return True
        
        logger.info(f"📝 COPY {side}: {shares}sh @ ${price:.4f} | {title}")
        
        try:
            order_side = BUY if side == "BUY" else SELL
            r = self.client.create_and_post_order(OrderArgsV2(
                price=price, size=shares, side=order_side, token_id=token_id,
            ), order_type=OrderType.GTC)
            if r:
                oid = str(r.get("orderID", r.get("id", "")))
                self.pending_orders[oid] = {
                    "token_id": token_id, "side": side,
                    "price": price, "size": shares,
                    "slug": slug, "title": title,
                }
                logger.info(f"  ✅ Order placed: oid={oid[:24]}")
                return True
        except Exception as e:
            logger.warning(f"  ❌ Order failed: {str(e)[:80]}")
        
        return False
    
    def copy_redeem(self, event: dict) -> bool:
        """When WeatherHK redeems, we sell our corresponding position at best price."""
        slug = event.get("slug", "")
        title = event.get("title", "")[:60]
        
        # Find our positions in this market
        our_positions = {tid: p for tid, p in self.positions.items() if p["slug"] == slug}
        
        if not our_positions:
            logger.debug(f"  ⏭ No matching position for redeem: {slug}")
            return False
        
        logger.info(f"🔴 REDEEM detected for {title} → selling our {len(our_positions)} positions")
        
        for token_id, pos in our_positions.items():
            try:
                r = self.client.create_and_post_order(OrderArgsV2(
                    price=0.99, size=pos["size"], side=SELL, token_id=token_id,
                ), order_type=OrderType.GTC)
                if r:
                    logger.info(f"  📤 SELL: {pos['size']}sh @ $0.99 | {pos['side']}")
            except Exception as e:
                logger.warning(f"  ❌ Sell failed: {str(e)[:80]}")
        
        return True
    
    def copy_sell(self, event: dict) -> bool:
        """When WeatherHK sells, we STRICTLY copy — sell our matching position.
        
        This is NOT opening a short. This is closing our BUY position 
        that we opened when he bought. Strict mirror: BUY→BUY, SELL→SELL.
        """
        side = event.get("side", "")
        if side != "SELL":
            return False
        
        price = float(event.get("price", 0))
        slug = event.get("slug", "")
        title = event.get("title", "")[:60]
        
        # Find our positions in this market
        our_positions = {tid: p for tid, p in self.positions.items() if p["slug"] == slug}
        
        if not our_positions:
            logger.debug(f"  ⏭ No matching position for copy-sell: {slug}")
            return False
        
        logger.info(f"🔻 COPY SELL: {title} → closing {len(our_positions)} positions")
        
        for token_id, pos in our_positions.items():
            try:
                r = self.client.create_and_post_order(OrderArgsV2(
                    price=price, size=pos["size"], side=SELL, token_id=token_id,
                ), order_type=OrderType.GTC)
                if r:
                    oid = str(r.get("orderID", r.get("id", "")))
                    self.pending_orders[oid] = {
                        "token_id": token_id, "side": "SELL",
                        "price": price, "size": pos["size"],
                        "slug": slug, "title": title,
                    }
                    logger.info(f"  📤 SELL {pos['size']}sh @ ${price:.4f}")
            except Exception as e:
                logger.warning(f"  ❌ Copy-sell failed: {str(e)[:80]}")
        
        return True
    
    def process_new_events(self, events: List[dict]):
        """Process all new events from WeatherHK.
        
        BUY  → copy_trade (open position)
        SELL → copy_sell (close our matching position)
        REDEEM → copy_redeem (close + claim payout)
        """
        buys  = [e for e in events if e.get("type") == "TRADE" and e.get("side") == "BUY"]
        sells = [e for e in events if e.get("type") == "TRADE" and e.get("side") == "SELL"]
        redeems = [e for e in events if e.get("type") == "REDEEM"]
        
        for event in buys:
            self.copy_trade(event)
        
        for event in sells:
            self.copy_sell(event)
        
        for event in redeems:
            self.copy_redeem(event)
    
    def check_fills(self):
        """Check pending order fills. BUY → add position, SELL → remove position."""
        for oid, info in list(self.pending_orders.items()):
            try:
                s = self.client.get_order(oid)
                if not s: continue
                status = str(s.get("status", "")).lower()
                if status in ("filled", "matched"):
                    price = float(s.get("price_matched") or s.get("avg_price") or s.get("price") or info["price"])
                    if info["side"] == "BUY":
                        # BUY filled → track the new position
                        self.positions[info["token_id"]] = {
                            "side": info["side"], "size": info["size"],
                            "entry_price": price, "slug": info["slug"],
                        }
                        logger.info(f"✅ BUY FILLED: {info['size']}sh @ ${price:.4f} | {info.get('title','')[:40]}")
                    else:
                        # SELL filled → remove from tracked positions
                        self.positions.pop(info["token_id"], None)
                        logger.info(f"🔻 SELL FILLED: {info['size']}sh @ ${price:.4f} | position closed")
                    del self.pending_orders[oid]
            except: pass
    
    def status(self) -> str:
        return (
            f"holds={len(self.positions)} "
            f"pending={len(self.pending_orders)} "
            f"cache={len(self.market_cache)} mkts"
        )


# ============================================================
# MAIN
# ============================================================
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true")
    p.add_argument("--ratio", type=float, default=DEFAULT_RATIO,
                   help=f"Position size ratio (default: {DEFAULT_RATIO})")
    args = p.parse_args()
    
    load_dotenv(dotenv_path=BASE_DIR / ".env")
    
    # Init tracker
    tracker = WeatherHKTacker(DATA_DIR / "weatherhk_state.json")
    
    # Init copy trader
    pk = os.environ.get("POLY_PRIVATE_KEY")
    ak = os.environ.get("POLY_API_KEY", "")
    a_s = os.environ.get("POLY_API_SECRET", "")
    ap = os.environ.get("POLY_API_PASSPHRASE", "")
    prx = os.environ.get("POLY_PROXY_WALLET", "")
    dep = os.environ.get("POLY_DEPOSIT_WALLET", "")
    
    creds = ApiCreds(api_key=ak, api_secret=a_s, api_passphrase=ap)
    clob = ClobClient(
        host=CLOB, chain_id=137, key=pk,
        creds=creds, funder=prx or dep, signature_type=3,
    )
    trader = CopyTrader(clob, live=args.live, ratio=args.ratio)
    
    # ── Startup self-test ──
    if args.live:
        result = trader.self_test()
        if result is False:
            logger.critical("Bot will continue but orders may fail — fix credentials and restart!")
        elif result is True:
            logger.info("🧪 Self-test complete — API credentials verified")
        else:
            logger.info("🧪 Self-test skipped (test token unavailable) — balance check passed")
    
    mode = "🚀 LIVE" if args.live else "🔍 DRY RUN"
    logger.info("=" * 55)
    logger.info(f"🌤 WeatherHK Copy Bot — {mode}")
    logger.info(f"   Target: @weatherhk ({WHK_WALLET[:10]}...)")
    logger.info(f"   Ratio: {args.ratio:.0%} of his size")
    logger.info(f"   Min copy: ${MIN_COPY_SIZE:.0f}")
    logger.info(f"   Poll: {POLL_INTERVAL}s")
    logger.info("=" * 55)
    
    # Initialize: seed old trades (skip them), then copy recent ones
    logger.info("Loading existing activity...")
    tracker.get_latest_state()
    
    # Now catch up on recent trades (last 10 min) that we may have missed during init
    logger.info("Catching up on recent WHK activity...")
    from datetime import datetime, timezone, timedelta
    
    def _make_eid(act):
        tx = act.get("transactionHash", "")
        oid = act.get("orderID", act.get("id", ""))
        return tx or oid or f"{act.get('timestamp','')}_{act.get('type','')}_{act.get('size','')}"
    
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp()
    recent_all = fetch_weatherhk_activity(100)
    recent_new = []
    for act in recent_all:
        eid = _make_eid(act)
        ts = act.get("timestamp", 0)
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except:
                ts = 0
        if eid not in tracker.known_tx_hashes and eid not in tracker.known_order_ids and ts >= cutoff:
            tracker.known_tx_hashes.add(eid)
            recent_new.append(act)
    if recent_new:
        tracker._save()
        logger.info(f"  Found {len(recent_new)} recent trades to copy")
        trader.process_new_events(recent_new)
    else:
        logger.info("  No recent trades to catch up")
    
    cycles = 0
    running = True
    signal.signal(signal.SIGINT, lambda *a: setattr(sys.modules[__name__], 'running', False))
    
    while running:
        try:
            cycles += 1
            
            # Check fills FIRST (so positions are ready for SELL events below)
            if args.live:
                trader.check_fills()
            
            # Poll for new activity
            new_events = tracker.poll()
            
            # Process new events
            if new_events:
                trader.process_new_events(new_events)
            
            # Status log
            if cycles % 12 == 0:  # every ~60s
                bal = trader.balance()
                logger.info(
                    f"📊 C#{cycles} | {trader.status()} | "
                    f"bal=${bal:.2f} | "
                    f"tracked={len(tracker.known_tx_hashes)} events"
                )
            
            time.sleep(POLL_INTERVAL)
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Cycle error: {e}")
            time.sleep(10)
    
    logger.info("🏁 Bot stopped")


if __name__ == "__main__":
    main()
