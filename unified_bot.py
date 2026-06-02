"""
🎯 Unified Trading Bot v2 — Micro-Arb + Momentum Trend

Optimized: fast curl, parallel order book checks, realistic thresholds.

Strategy 1 — Micro-Arbitrage:
  Pull order books for current-window markets only (BTC/ETH/SOL/XRP).
  When UP ask + DOWN ask < 0.988, buy both sides simultaneously.
  
Strategy 2 — Momentum Trend:
  Track BTC/ETH prices every 5s from Binance.
  When sustained >0.3% momentum builds in 60s, bet on continuation
  if we're early in a 5-min window.

Usage:
  python3 unified_bot.py                  # Dry run
  python3 unified_bot.py --live           # Live trading
  python3 unified_bot.py --live --size 3  # $3 per trade
"""

import os, sys, json, time, signal, logging, argparse, subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from py_clob_client_v2 import ClobClient, ApiCreds
from py_clob_client_v2.clob_types import (
    OrderArgs, OrderType, BalanceAllowanceParams, AssetType,
)
from py_clob_client_v2.order_builder.constants import BUY

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"
for d in [LOG_DIR, DATA_DIR]:
    d.mkdir(parents=True, exist_ok=True)

GAMMA_API    = "https://gamma-api.polymarket.com"
CLOB_HOST    = "https://clob.polymarket.com"
BINANCE_API  = "https://api.binance.com"

# Strategy params
ARB_THRESHOLD     = 0.988   # must be below this (covers 2×0.1% fee + edge)
ARB_MIN_PROFIT    = 0.06
MOMENTUM_WINDOW   = 60
MOMENTUM_MIN      = 0.003   # 0.3%
MOMENTUM_EARLY_S  = 120
TAKER_FEE_BPS     = 10

# Risk
DEFAULT_SIZE      = 5.0
MAX_TRADE_SIZE    = 25.0
DAILY_LOSS_LIMIT  = 50.0
MAX_CONSEC_LOSS   = 5
MIN_BALANCE       = 5.0
MAX_OPEN          = 8

ASSETS = ["btc", "eth", "sol", "xrp"]
CURL_FAST = ["curl", "-s", "--connect-timeout", "2", "--max-time", "4"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "unified_bot.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("unified_bot")


# ============================================================
def curl_fast(url: str) -> Optional[dict]:
    try:
        r = subprocess.run(CURL_FAST + [url],
                           capture_output=True, text=True, timeout=6)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except:
        pass
    return None


def now_utc(): return datetime.now(timezone.utc)


# ============================================================
# RISK
# ============================================================
class RiskManager:
    def __init__(self):
        self.daily_pnl = self.consec_losses = self.open_positions = 0
        self.total_trades = self.total_wins = 0
        self.total_pnl = self.daily_date = ""
        self._f = DATA_DIR / "unified_state.json"
        self._load()

    def _load(self):
        if self._f.exists():
            try:
                d = json.loads(self._f.read_text())
                for k in ["daily_pnl","daily_date","consec_losses",
                          "open_positions","total_trades","total_wins","total_pnl"]:
                    setattr(self, k, d.get(k, getattr(self, k)))
            except: pass

    def _save(self):
        self._f.write_text(json.dumps({
            "daily_pnl": self.daily_pnl, "daily_date": self.daily_date,
            "consec_losses": self.consec_losses,
            "open_positions": self.open_positions,
            "total_trades": self.total_trades, "total_wins": self.total_wins,
            "total_pnl": self.total_pnl, "updated": now_utc().isoformat(),
        }, indent=2))

    def check(self, client) -> Tuple[bool, str]:
        today = now_utc().strftime("%Y-%m-%d")
        if self.daily_date != today:
            self.daily_pnl = 0; self.daily_date = today
        if self.consec_losses >= MAX_CONSEC_LOSS:
            return False, f"consec losses {self.consec_losses}"
        if self.daily_pnl <= -DAILY_LOSS_LIMIT:
            return False, f"daily loss ${abs(self.daily_pnl):.0f}"
        if self.open_positions >= MAX_OPEN:
            return False, f"positions {self.open_positions}"
        try:
            bal = float(client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            ).get("balance", 0)) / 1e6
        except: bal = 0
        if bal < MIN_BALANCE:
            return False, f"bal ${bal:.2f}"
        return True, "OK"

    def record(self, pnl: float):
        self.total_trades += 1; self.total_pnl += pnl
        self.daily_pnl += pnl
        if pnl > 0: self.total_wins += 1; self.consec_losses = 0
        else: self.consec_losses += 1
        self._save()


# ============================================================
# STRATEGY 1: MICRO-ARB (order book only, no gamma pre-filter)
# ============================================================
def scan_current_markets() -> List[dict]:
    """Get active 5-min markets for current + next window only (fast)."""
    t0 = now_utc()
    base = t0.replace(second=0, microsecond=0,
                      minute=(t0.minute // 5) * 5)
    markets = []
    seen = set()

    for asset in ASSETS:
        prefix = f"{asset}-updown-5m"
        for offset in [-5, 0, 5]:  # prev, current, next — 3 windows only
            ts = int((base + timedelta(minutes=offset)).timestamp())
            slug = f"{prefix}-{ts}"
            if slug in seen: continue
            seen.add(slug)

            data = curl_fast(f"{GAMMA_API}/markets?slug={slug}")
            if not data or not isinstance(data, list) or not data:
                continue
            m = data[0]
            if not m.get("active") or m.get("closed"):
                continue
            prices = json.loads(m.get("outcomePrices", "[]"))
            tokens = json.loads(m.get("clobTokenIds", "[]"))
            if len(tokens) < 2: continue
            markets.append({
                "question": m.get("question", ""),
                "slug": slug, "asset": asset,
                "condition_id": m.get("conditionId", ""),
                "token_up": tokens[0], "token_down": tokens[1],
                "gamma_up": float(prices[0]) if prices else 0.5,
                "gamma_down": float(prices[1]) if len(prices) >= 2 else 0.5,
                "volume": float(m.get("volume", 0)),
                "end_date": m.get("endDate", ""),
            })
    return markets


def check_arb(mkt: dict) -> Optional[dict]:
    """Check order books for UP+DOWN arbitrage. Returns signal or None.
    Also sets 'last_sum' and 'last_ts' on mkt for monitoring."""
    import concurrent.futures
    def _book(token):
        return curl_fast(f"{CLOB_HOST}/book?token_id={token}")

    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_up = ex.submit(_book, mkt["token_up"])
        fut_dn = ex.submit(_book, mkt["token_down"])
        ob_up = fut_up.result(timeout=5)
        ob_dn = fut_dn.result(timeout=5)

    if not ob_up or not ob_dn: return None
    au = ob_up.get("asks", [])
    ad = ob_dn.get("asks", [])
    if not au or not ad: return None

    ask_up = float(au[0].get("price", 0.99))
    ask_dn = float(ad[0].get("price", 0.99))
    sz_up = float(au[0].get("size", 0))
    sz_dn = float(ad[0].get("size", 0))
    total = ask_up + ask_dn

    # Track for monitoring
    mkt["_ask_sum"] = total
    mkt["_ask_up"] = ask_up
    mkt["_ask_down"] = ask_dn

    if total >= ARB_THRESHOLD: return None
    max_sz = min(sz_up, sz_dn, MAX_TRADE_SIZE)
    if max_sz < 1: return None

    fee = (TAKER_FEE_BPS * 2) / 10000
    net_pct = (1.0 - total) - fee
    if net_pct <= 0: return None
    profit = max_sz * net_pct
    if profit < ARB_MIN_PROFIT: return None

    return {
        "market": mkt, "ask_up": ask_up, "ask_down": ask_dn,
        "total_cost": total, "net_pct": net_pct,
        "size": max_sz, "est_profit": profit,
        "ts": now_utc().isoformat(),
    }


# ============================================================
# STRATEGY 2: MOMENTUM
# ============================================================
class PriceTracker:
    def __init__(self):
        self.data: Dict[str, List[Tuple[float, float]]] = {"btc": [], "eth": []}
        self._symbols = {"btc": "BTCUSDT", "eth": "ETHUSDT"}
        self._fail_count = {"btc": 0, "eth": 0}

    def update(self):
        for asset, sym in self._symbols.items():
            d = curl_fast(f"{BINANCE_API}/api/v3/ticker/price?symbol={sym}")
            if d and "price" in d:
                price = float(d["price"])
                self.data[asset].append((time.time(), price))
                self._fail_count[asset] = 0
            else:
                self._fail_count[asset] += 1
            # Trim
            cutoff = time.time() - 600
            self.data[asset] = [p for p in self.data[asset] if p[0] > cutoff]

    def momentum(self, asset: str) -> Optional[float]:
        pts = self.data.get(asset, [])
        if len(pts) < 3: return None
        cutoff = time.time() - MOMENTUM_WINDOW
        old = [p for p in pts if p[0] <= cutoff]
        if not old: return None
        return (pts[-1][1] - old[-1][1]) / old[-1][1]

    @property
    def ready(self) -> bool:
        return all(len(self.data.get(a, [])) >= 5 for a in ["btc", "eth"])


class MomentumDetector:
    def detect(self, mkt: dict, tracker: PriceTracker) -> Optional[dict]:
        asset = mkt.get("asset")
        if asset not in ("btc", "eth"): return None
        if not tracker.ready: return None

        t0 = now_utc()
        win_s = (t0.minute % 5) * 60 + t0.second
        if win_s > MOMENTUM_EARLY_S: return None

        mom = tracker.momentum(asset)
        if mom is None or abs(mom) < MOMENTUM_MIN: return None

        direction = "Up" if mom > 0 else "Down"
        token = mkt["token_up"] if direction == "Up" else mkt["token_down"]

        ob = curl_fast(f"{CLOB_HOST}/book?token_id={token}")
        if not ob: return None
        asks = ob.get("asks", [])
        if not asks: return None

        price = float(asks[0]["price"])
        # Require reasonable price (not >0.80 — too late to enter)
        if price > 0.70: return None

        sz = min(float(asks[0]["size"]), MAX_TRADE_SIZE)
        if sz < 1: return None

        return {
            "market": mkt, "direction": direction, "token": token,
            "price": price, "size": sz, "momentum": mom,
            "reason": f"{asset.upper()} {direction} {mom*100:+.2f}% @{win_s}s",
            "ts": t0.isoformat(),
        }


# ============================================================
# EXECUTOR
# ============================================================
class Executor:
    def __init__(self, client: ClobClient, risk: RiskManager,
                 live: bool = False, size: float = DEFAULT_SIZE):
        self.client = client; self.risk = risk
        self.live = live; self.size = size

    def balance(self) -> float:
        try:
            return float(self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            ).get("balance", 0)) / 1e6
        except: return 0

    def exec_arb(self, arb: dict) -> Optional[dict]:
        ok, why = self.risk.check(self.client)
        if not ok:
            logger.info(f"⛔ ARB block: {why}"); return None

        mkt = arb["market"]; sz = min(self.size, arb["size"])
        cost = sz * (arb["ask_up"] + arb["ask_down"])

        if not self.live:
            logger.info(
                f"🔍 ARB: {sz:.1f} UP@{arb['ask_up']:.4f}+DN@{arb['ask_down']:.4f} "
                f"=${cost:.2f} +${sz*arb['net_pct']:.2f} | {mkt['slug']}"
            )
            return {"dry": True}

        logger.info(f"⚡ LIVE ARB: {sz:.1f}/side | {mkt['slug']}")
        try:
            ru = self.client.create_and_post_order(OrderArgs(
                price=arb["ask_up"], size=sz, side=BUY,
                token_id=mkt["token_up"]), OrderType.GTC)
            rd = self.client.create_and_post_order(OrderArgs(
                price=arb["ask_down"], size=sz, side=BUY,
                token_id=mkt["token_down"]), OrderType.GTC)
            ok = ru is not None and rd is not None
            self.risk.record(sz * arb["net_pct"] if ok else -0.05)
            logger.info(f"{'✅' if ok else '❌'} UP={'OK' if ru else 'FAIL'} DN={'OK' if rd else 'FAIL'}")
            return {"ok": ok, "type": "arb", "slug": mkt["slug"]}
        except Exception as e:
            logger.error(f"ARB fail: {e}"); return None

    def exec_mom(self, sig: dict) -> Optional[dict]:
        ok, why = self.risk.check(self.client)
        if not ok: return None

        sz = min(self.size, sig["size"])
        if not self.live:
            logger.info(
                f"🔍 MOM: {sz:.1f} {sig['direction']} @{sig['price']:.4f} "
                f"| {sig['reason']}"
            )
            return {"dry": True}

        logger.info(f"📈 LIVE MOM: {sig['direction']} | {sig['reason']}")
        try:
            r = self.client.create_and_post_order(OrderArgs(
                price=sig["price"], size=sz, side=BUY,
                token_id=sig["token"]), OrderType.GTC)
            self.risk.open_positions += 1
            self.risk._save()
            logger.info(f"{'✅' if r else '❌'} {sig['direction']}")
            return {"ok": r is not None, "type": "momentum"}
        except Exception as e:
            logger.error(f"MOM fail: {e}"); return None


# ============================================================
# UNIFIED BOT
# ============================================================
class UnifiedBot:
    def __init__(self, live=False, size=DEFAULT_SIZE):
        load_dotenv(dotenv_path=BASE_DIR / ".env")
        self.live = live; self.size = size; self.running = True

        pk = os.environ.get("POLY_PRIVATE_KEY")
        ak = os.environ.get("POLY_API_KEY", "")
        a_s = os.environ.get("POLY_API_SECRET", "")
        ap = os.environ.get("POLY_API_PASSPHRASE", "")
        prx = os.environ.get("POLY_PROXY_WALLET", "")
        dep = os.environ.get("POLY_DEPOSIT_WALLET", "")
        creds = ApiCreds(api_key=ak, api_secret=a_s, api_passphrase=ap)
        self.clob = ClobClient(
            host=CLOB_HOST, chain_id=137, key=pk,
            creds=creds, funder=prx or dep, signature_type=3)

        self.risk = RiskManager()
        self.tracker = PriceTracker()
        self.mom_det = MomentumDetector()
        self.executor = Executor(self.clob, self.risk, live=live, size=size)

        self.cycles = self.arbs_f = self.arbs_d = self.mom_s = self.mom_t = 0
        signal.signal(signal.SIGINT, lambda *a: setattr(self, 'running', False))

    def step(self):
        markets = scan_current_markets()
        if not markets: return

        best_sum = 99.0
        best_name = ""

        # Strategy 1: Micro-Arb — check order books for all markets
        for mkt in markets:
            arb = check_arb(mkt)
            if arb:
                self.arbs_f += 1
                logger.info(
                    f"🚨 ARB #{self.arbs_f}: {mkt['slug']} "
                    f"UP={arb['ask_up']:.4f} DN={arb['ask_down']:.4f} "
                    f"SUM={arb['total_cost']:.4f} +${arb['est_profit']:.2f}"
                )
                r = self.executor.exec_arb(arb)
                if r: self.arbs_d += 1
                time.sleep(0.3)
            # Track best spread from the check we already did
            s = mkt.get("_ask_sum")
            if s is not None and s < best_sum:
                best_sum = s; best_name = mkt["slug"]

        # Show spread summary (uses cached values from check_arb)
        if self.cycles <= 3 or self.cycles % 3 == 0:
            mom_btc = self.tracker.momentum("btc")
            mom_eth = self.tracker.momentum("eth")
            if best_name:
                logger.info(
                    f"👁  Best spread: {best_name} "
                    f"SUM={best_sum:.4f} "
                    f"(threshold={ARB_THRESHOLD}) | "
                    f"BTC mom={'n/a' if mom_btc is None else f'{mom_btc*100:+.2f}%'} "
                    f"ETH mom={'n/a' if mom_eth is None else f'{mom_eth*100:+.2f}%'}"
                )

        # Strategy 2: Momentum — check current-window markets only
        for mkt in markets:
            sig = self.mom_det.detect(mkt, self.tracker)
            if sig:
                self.mom_s += 1
                logger.info(f"📈 MOM #{self.mom_s}: {sig['reason']}")
                r = self.executor.exec_mom(sig)
                if r: self.mom_t += 1
                time.sleep(0.2)

    def run(self, interval=3.0):
        mode = "🚀 LIVE" if self.live else "🔍 DRY RUN"
        bal = self.executor.balance()
        logger.info("=" * 55)
        logger.info(f"🎯 Unified Bot v2 — {mode}")
        logger.info(f"   Arb: UP+DN < {ARB_THRESHOLD}  |  Mom: >{MOMENTUM_MIN*100:.1f}%")
        logger.info(f"   Size: ${self.size}  |  Balance: ${bal:.2f}")
        logger.info("=" * 55)

        # Warm-up price tracker
        logger.info("⏳ Warming up (price feeds + market scan)...")
        for i in range(3):
            self.tracker.update(); time.sleep(3.5)
        # Warm-up first scan to prime cache
        scan_current_markets()
        logger.info("✅ Warmup complete, monitoring...")

        while self.running:
            try:
                self.cycles += 1
                self.tracker.update()
                t0 = time.time()
                self.step()
                elapsed = time.time() - t0

                # Show status every cycle for the first 3, then every 5
                show = self.cycles <= 3 or self.cycles % 5 == 0
                if show:
                    bal = self.executor.balance()
                    wr = (self.risk.total_wins / self.risk.total_trades * 100
                          if self.risk.total_trades else 0)
                    logger.info(
                        f"📊 C#{self.cycles} ({elapsed:.1f}s) | "
                        f"ARB {self.arbs_f}f/{self.arbs_d}d | "
                        f"MOM {self.mom_s}s/{self.mom_t}t | "
                        f"PnL ${self.risk.total_pnl:+.2f} WR={wr:.0f}% bal=${bal:.2f}"
                    )

                sleep_t = max(0.5, interval - elapsed)
                time.sleep(sleep_t)
            except KeyboardInterrupt: break
            except Exception as e:
                logger.error(f"Cycle err: {e}")
                time.sleep(5)

        logger.info(f"🏁 ARB {self.arbs_f}f/{self.arbs_d}d MOM {self.mom_s}s/{self.mom_t}t PnL=${self.risk.total_pnl:+.2f}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true")
    p.add_argument("--size", type=float, default=DEFAULT_SIZE)
    p.add_argument("--interval", type=float, default=3.0)
    a = p.parse_args()
    UnifiedBot(live=a.live, size=a.size).run(interval=a.interval)

if __name__ == "__main__":
    main()
