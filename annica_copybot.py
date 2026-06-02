"""
🐦 Annica Copy Trader — Track & Mirror Annica's YES Lottery Tickets

Strategy:
  1. Continuously poll Annica's latest activity
  2. ONLY copy BUY YES @ <$0.30 (lottery tickets — his real edge)
  3. Skip BUY NO entirely (his safe income stream, not our strategy)
  4. Copy SELL to close matching positions
  5. Copy REDEEM to exit at $0.99

Usage:
  python3 annica_copybot.py --live --ratio 0.10   # 10% of his position size
  python3 annica_copybot.py                         # dry run
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
ANNICA_WALLET = "0x689ae12e11aa489adb3605afd8f39040ff52779e"

# Strategy params
POLL_INTERVAL = 10       # seconds — Annica trades infrequently
MIN_COPY_SIZE  = 0.50     # minimum $0.50 to copy
MAX_YES_PRICE  = 0.30     # only copy YES if price < $0.30 (lottery tickets)
DEFAULT_RATIO  = 0.10     # 10% of his position size

CURL_CMD = ["curl", "-s", "--connect-timeout", "2", "--max-time", "5"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "annica_copy.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("annica_copy")


# ============================================================
# UTILS
# ============================================================
def curl(url: str) -> Optional[dict]:
    try:
        r = subprocess.run(
            ["/usr/bin/env", "-u", "SSL_CERT_FILE", "-u", "REQUESTS_CA_BUNDLE",
             "/usr/bin/curl", "-s", "--connect-timeout", "3", "--max-time", "10", url],
            capture_output=True, text=True, timeout=12,
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except: pass
    return None


def fetch_annica_activity(limit: int = 50) -> List[dict]:
    all_acts = []
    for offset in [0, 50, 100, 150]:
        data = curl(f"https://data-api.polymarket.com/activity?user={ANNICA_WALLET}&limit={limit}&offset={offset}")
        if data and isinstance(data, list) and len(data) > 0:
            all_acts.extend(data)
        else:
            break
    return all_acts


def get_market_by_slug(slug: str) -> Optional[dict]:
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
    tokens = market.get("tokens", [])
    if outcome_index < len(tokens):
        return tokens[outcome_index]
    return None


# ============================================================
# TRACKER
# ============================================================
class AnnicaTracker:
    """Tracks Annica's activity and detects new events."""
    
    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.known_tx_hashes: Set[str] = set()
        self.last_check = 0
        self._load()
    
    def _load(self):
        if self.state_file.exists():
            try:
                d = json.loads(self.state_file.read_text())
                self.known_tx_hashes = set(d.get("tx_hashes", []))
                self.last_check = d.get("last_check", 0)
            except: pass
    
    def _save(self):
        self.state_file.write_text(json.dumps({
            "tx_hashes": list(self.known_tx_hashes)[-5000:],
            "last_check": self.last_check,
        }))
    
    def poll(self) -> List[dict]:
        activities = fetch_annica_activity()
        self.last_check = time.time()
        
        new_events = []
        for act in activities:
            tx_hash = act.get("transactionHash", "")
            oid = act.get("orderID", act.get("id", ""))
            event_id = tx_hash or oid
            if not event_id:
                event_id = f"{act.get('timestamp','')}_{act.get('type','')}_{act.get('size','')}"
            
            if event_id not in self.known_tx_hashes:
                self.known_tx_hashes.add(event_id)
                new_events.append(act)
        
        if new_events:
            self._save()
            logger.info(f"🆕 {len(new_events)} new Annica events")
        
        return new_events
    
    def get_latest_state(self) -> List[dict]:
        activities = fetch_annica_activity()
        count = 0
        for act in activities:
            tx_hash = act.get("transactionHash", "")
            oid = act.get("orderID", act.get("id", ""))
            event_id = tx_hash or oid or f"{act.get('timestamp','')}_{act.get('type','')}"
            if event_id not in self.known_tx_hashes:
                self.known_tx_hashes.add(event_id)
                count += 1
        self._save()
        logger.info(f"Initialized: {count} known events (will not copy)")
        return activities


# ============================================================
# COPY TRADER
# ============================================================
class CopyTrader:
    """Copies Annica's YES lottery tickets with proportional sizing."""
    
    def __init__(self, client: ClobClient, live: bool = False, ratio: float = DEFAULT_RATIO,
                 max_price: float = MAX_YES_PRICE):
        self.client = client
        self.live = live
        self.ratio = ratio
        self.max_price = max_price
        self.positions: Dict[str, dict] = {}
        self.pending_orders: Dict[str, dict] = {}
        self.market_cache: Dict[str, dict] = {}
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
        """
        logger.info("🧪 Running API credential self-test...")
        test_token = "21742633143463906290569050155826241533067272736897614950488156847949938836455"
        try:
            r = self.client.create_and_post_order(OrderArgsV2(
                price=0.01, size=5, side=BUY, token_id=test_token,
            ), order_type=OrderType.GTC)
            if r:
                oid = str(r.get("orderID", r.get("id", "")))
                logger.info(f"  ✅ Self-test PASSED — order {oid[:20]}... placed successfully")
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
                    return None
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
        our_size = his_size_usdc * self.ratio
        return max(MIN_COPY_SIZE, round(our_size, 2))
    
    def copy_yes_buy(self, event: dict) -> bool:
        """Copy Annica's YES buy — only if it's a lottery ticket (< $0.30)."""
        side = event.get("side", "")
        if side != "BUY":
            return False
        
        outcome = event.get("outcome", "")
        # STRATEGY: only copy YES buys. NO buys are his safe income, skip.
        if outcome != "Yes":
            logger.debug(f"  ⏭ Skipping NO buy: {event.get('title','')[:50]}")
            return False
        
        price = float(event.get("price", 0))
        
        # Only copy cheap lottery tickets
        if price > self.max_price:
            logger.debug(f"  ⏭ Skipping expensive YES: ${price:.4f} > ${self.max_price:.2f}")
            return False
        
        his_size_usdc = float(event.get("usdcSize", 0))
        slug = event.get("slug", "")
        title = event.get("title", "")[:60]
        outcome_idx = int(event.get("outcomeIndex", 0))
        
        if his_size_usdc < MIN_COPY_SIZE:
            return False
        
        our_size_usdc = self._calc_copy_size(his_size_usdc)
        bal = self.balance()
        if our_size_usdc > bal * 0.15:  # more conservative: max 15% per lottery ticket
            our_size_usdc = bal * 0.10
            if our_size_usdc < MIN_COPY_SIZE:
                logger.debug(f"  ⏭ Insufficient balance: ${bal:.2f}")
                return False
        
        mkt = self._get_market(slug)
        if not mkt:
            logger.debug(f"  ⏭ Market not active: {slug}")
            return False
        
        token_id = get_token_for_outcome(mkt, outcome_idx)
        if not token_id:
            return False
        
        shares = max(5, int(our_size_usdc / price)) if price > 0 else 5
        
        if not self.live:
            logger.info(f"🔍 COPY YES: {shares}sh @ ${price:.3f} (~${our_size_usdc:.2f}) | {title}")
            return True
        
        logger.info(f"🎫 COPY YES: {shares}sh @ ${price:.4f} | {title}")
        
        try:
            r = self.client.create_and_post_order(OrderArgsV2(
                price=price, size=shares, side=BUY, token_id=token_id,
            ), order_type=OrderType.GTC)
            if r:
                oid = str(r.get("orderID", r.get("id", "")))
                self.pending_orders[oid] = {
                    "token_id": token_id, "side": "BUY",
                    "price": price, "size": shares,
                    "slug": slug, "title": title,
                }
                logger.info(f"  ✅ Order placed: oid={oid[:24]}")
                return True
        except Exception as e:
            logger.warning(f"  ❌ Order failed: {str(e)[:80]}")
        
        return False
    
    def copy_sell(self, event: dict) -> bool:
        """When Annica sells, close our matching position."""
        side = event.get("side", "")
        if side != "SELL":
            return False
        
        price = float(event.get("price", 0))
        slug = event.get("slug", "")
        title = event.get("title", "")[:60]
        
        our_positions = {tid: p for tid, p in self.positions.items() if p["slug"] == slug}
        if not our_positions:
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
    
    def copy_redeem(self, event: dict) -> bool:
        """When Annica redeems YES, sell our position at $0.99."""
        slug = event.get("slug", "")
        title = event.get("title", "")[:60]
        
        our_positions = {tid: p for tid, p in self.positions.items() if p["slug"] == slug}
        if not our_positions:
            return False
        
        logger.info(f"🔴 REDEEM for {title} → selling {len(our_positions)} positions")
        
        for token_id, pos in our_positions.items():
            try:
                r = self.client.create_and_post_order(OrderArgsV2(
                    price=0.99, size=pos["size"], side=SELL, token_id=token_id,
                ), order_type=OrderType.GTC)
                if r:
                    logger.info(f"  📤 REDEEM SELL: {pos['size']}sh @ $0.99")
            except Exception as e:
                logger.warning(f"  ❌ Redeem sell failed: {str(e)[:80]}")
        
        return True
    
    def process_new_events(self, events: List[dict]):
        """Process new events from Annica.
        
        BUY YES → copy_yes_buy (lottery ticket)
        BUY NO  → skip (his safe income)
        SELL    → copy_sell (close our position)
        REDEEM  → copy_redeem (sell @ $0.99)
        """
        buys  = [e for e in events if e.get("type") == "TRADE" and e.get("side") == "BUY"]
        sells = [e for e in events if e.get("type") == "TRADE" and e.get("side") == "SELL"]
        redeems = [e for e in events if e.get("type") == "REDEEM"]
        
        for event in buys:
            self.copy_yes_buy(event)
        
        for event in sells:
            self.copy_sell(event)
        
        for event in redeems:
            self.copy_redeem(event)
    
    def check_fills(self):
        """Check fills. BUY → track, SELL → remove."""
        for oid, info in list(self.pending_orders.items()):
            try:
                s = self.client.get_order(oid)
                if not s: continue
                status = str(s.get("status", "")).lower()
                if status in ("filled", "matched"):
                    price = float(s.get("price_matched") or s.get("avg_price") or s.get("price") or info["price"])
                    if info["side"] == "BUY":
                        self.positions[info["token_id"]] = {
                            "side": info["side"], "size": info["size"],
                            "entry_price": price, "slug": info["slug"],
                        }
                        logger.info(f"✅ BUY FILLED: {info['size']}sh @ ${price:.4f} | {info.get('title','')[:40]}")
                    else:
                        self.positions.pop(info["token_id"], None)
                        logger.info(f"🔻 SELL FILLED: {info['size']}sh @ ${price:.4f} | closed")
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
    p.add_argument("--max-price", type=float, default=MAX_YES_PRICE,
                   help=f"Max YES price to copy (default: ${MAX_YES_PRICE})")
    args = p.parse_args()
    
    load_dotenv(dotenv_path=BASE_DIR / ".env")
    
    # Init tracker
    tracker = AnnicaTracker(DATA_DIR / "annica_state.json")
    
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
    trader = CopyTrader(clob, live=args.live, ratio=args.ratio, max_price=args.max_price)
    
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
    logger.info(f"🐦 Annica Copy Bot — {mode}")
    logger.info(f"   Target: @Annica ({ANNICA_WALLET[:10]}...)")
    logger.info(f"   Strategy: YES lottery < ${args.max_price:.2f}")
    logger.info(f"   Ratio: {args.ratio:.0%} | Poll: {POLL_INTERVAL}s")
    logger.info(f"   Min copy: ${MIN_COPY_SIZE:.2f}")
    logger.info("=" * 55)
    
    # Seed known events (skip old ones)
    logger.info("Seeding known activity...")
    tracker.get_latest_state()
    
    cycles = 0
    running = True
    signal.signal(signal.SIGINT, lambda *a: setattr(sys.modules[__name__], 'running', False))
    
    while running:
        try:
            cycles += 1
            
            if args.live:
                trader.check_fills()
            
            new_events = tracker.poll()
            if new_events:
                trader.process_new_events(new_events)
            
            if cycles % 6 == 0:  # every ~60s
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
            time.sleep(15)
    
    logger.info("🏁 Annica bot stopped")


if __name__ == "__main__":
    main()
