"""
⚡ Polymarket 5-Min Micro Arbitrage Bot

Strategy: When BTC/ETH 5-min UP + DOWN token order book prices sum < $0.995,
simultaneously buy both sides for risk-free profit.

Ref: polymarket.com/profile/0x88f4... ($2,314/night, ~70% WR)
"""

import os, sys, json, time, signal, logging, argparse, subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple

from dotenv import load_dotenv
from py_clob_client_v2 import ClobClient, ApiCreds
from py_clob_client_v2.clob_types import (
    OrderArgs, OrderType, BalanceAllowanceParams, AssetType,
)
from py_clob_client_v2.order_builder.constants import BUY

# === Config ===
BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"
for d in [LOG_DIR, DATA_DIR]:
    d.mkdir(parents=True, exist_ok=True)

POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB = "https://clob.polymarket.com"
ARB_THRESHOLD = 0.995
MIN_PROFIT_USD = 0.10
DEFAULT_TRADE_SIZE = 5.0
MAX_TRADE_SIZE = 50.0
TAKER_FEE_BPS = 10
CONSECUTIVE_LOSS_LIMIT = 5
DAILY_LOSS_LIMIT = 50.0
MIN_BALANCE_USD = 5.0
CURL_TIMEOUT = 4  # seconds per curl call

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "micro_arb.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("micro_arb")


def curl_json(url: str, timeout: int = CURL_TIMEOUT) -> Optional[dict]:
    """Fetch JSON via curl (Python 3.14 urllib has SSL issues here)."""
    try:
        r = subprocess.run(
            ["curl", "-s", "--connect-timeout", str(timeout//2),
             "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 3,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return json.loads(r.stdout)
    except Exception as e:
        return None


# ============================================================
class MarketScanner:
    """Find BTC/ETH 5-min markets."""

    def __init__(self):
        self._cache = {}
        self._cache_time = 0

    def scan(self) -> List[dict]:
        now = time.time()
        if self._cache_time and now - self._cache_time < 3:
            return list(self._cache.values())

        now_dt = datetime.now(timezone.utc)
        c5 = now_dt.replace(second=0, microsecond=0)
        c5 = c5.replace(minute=(c5.minute // 5) * 5)

        markets = {}
        seen = set()
        for prefix in ["btc-updown-5m", "eth-updown-5m", "sol-updown-5m", "xrp-updown-5m"]:
            for off in range(-5, 15, 5):  # -5, 0, 5, 10 → 4 windows
                ts = int((c5 + timedelta(minutes=off)).timestamp())
                slug = f"{prefix}-{ts}"
                if slug in seen: continue
                seen.add(slug)

                data = curl_json(f"{POLYMARKET_GAMMA}/markets?slug={slug}")
                if not data or not isinstance(data, list) or not data:
                    continue
                m = data[0]
                if not m.get("active") or m.get("closed"):
                    continue

                prices = json.loads(m.get("outcomePrices", "[]"))
                tokens = json.loads(m.get("clobTokenIds", "[]"))
                if len(tokens) < 2: continue

                markets[slug] = {
                    "question": m.get("question", ""),
                    "slug": slug,
                    "condition_id": m.get("conditionId", ""),
                    "token_up": tokens[0],
                    "token_down": tokens[1],
                    "gamma_up": float(prices[0]) if len(prices) >= 2 else 0.5,
                    "gamma_down": float(prices[1]) if len(prices) >= 2 else 0.5,
                    "volume": float(m.get("volume", 0)),
                    "end_date": m.get("endDate", ""),
                }

        self._cache = markets
        self._cache_time = now
        return list(markets.values())


# ============================================================
class ArbDetector:
    """Check order books for UP + DOWN < threshold."""

    def __init__(self, client: ClobClient):
        self.client = client

    def check(self, mkt: dict) -> Optional[dict]:
        try:
            bu = self.client.get_order_book(mkt["token_up"])
            bd = self.client.get_order_book(mkt["token_down"])
        except Exception as e:
            return None

        if not bu or not bd: return None
        au = bu.get("asks", [])
        ad = bd.get("asks", [])
        if not au or not ad: return None

        ask_up = float(au[0].get("price", 0.99))
        ask_down = float(ad[0].get("price", 0.99))
        sz_up = float(au[0].get("size", 0))
        sz_down = float(ad[0].get("size", 0))
        total = ask_up + ask_down

        if total >= ARB_THRESHOLD: return None

        max_sz = min(sz_up, sz_down, MAX_TRADE_SIZE)
        if max_sz < 1: return None

        fee = (TAKER_FEE_BPS * 2) / 10000
        net = (1.0 - total) - fee
        if net <= 0: return None

        profit = max_sz * net
        if profit < MIN_PROFIT_USD: return None

        return {
            "market": mkt, "ask_up": ask_up, "ask_down": ask_down,
            "total_cost": total, "net_profit_pct": net,
            "available_size": max_sz, "est_profit": profit,
            "sz_up": sz_up, "sz_down": sz_down,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def prices(self, mkt: dict) -> dict:
        """Quick price snapshot."""
        try:
            bu = self.client.get_order_book(mkt["token_up"])
            bd = self.client.get_order_book(mkt["token_down"])
        except:
            return {}
        if not bu or not bd: return {}
        au, ad = bu.get("asks", []), bd.get("asks", [])
        bu_b, bd_b = bu.get("bids", []), bd.get("bids", [])
        return {
            "ask_up": float(au[0]["price"]) if au else None,
            "ask_down": float(ad[0]["price"]) if ad else None,
            "bid_up": float(bu_b[0]["price"]) if bu_b else None,
            "bid_down": float(bd_b[0]["price"]) if bd_b else None,
            "sum_ask": float(au[0]["price"]) + float(ad[0]["price"]) if au and ad else 1.0,
        }


# ============================================================
class ArbExecutor:
    """Execute arb trades."""

    def __init__(self, client: ClobClient, dry_run: bool = True):
        self.client = client
        self.dry_run = dry_run
        self.history: List[dict] = []
        self.daily_pnl = 0.0
        self.daily_date = ""
        self.consecutive_losses = 0
        self._load()

    def _load(self):
        sf = DATA_DIR / "arb_state.json"
        if sf.exists():
            try:
                s = json.loads(sf.read_text())
                self.history = s.get("trades", [])
                self.daily_pnl = s.get("daily_pnl", 0)
                self.daily_date = s.get("daily_date", "")
                self.consecutive_losses = s.get("consecutive_losses", 0)
            except: pass

    def _save(self):
        sf = DATA_DIR / "arb_state.json"
        sf.write_text(json.dumps({
            "trades": self.history[-200:],
            "daily_pnl": self.daily_pnl,
            "daily_date": self.daily_date,
            "consecutive_losses": self.consecutive_losses,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }, indent=2, default=str))

    def balance(self) -> float:
        try:
            info = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            return float(info.get("balance", 0)) / 1e6
        except: return 0.0

    def ok(self) -> Tuple[bool, str]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.daily_date != today:
            self.daily_pnl = 0; self.daily_date = today
        if self.consecutive_losses >= CONSECUTIVE_LOSS_LIMIT:
            return False, f"Consecutive losses {self.consecutive_losses}"
        if self.daily_pnl <= -DAILY_LOSS_LIMIT:
            return False, f"Daily loss ${abs(self.daily_pnl):.2f}"
        b = self.balance()
        if b < MIN_BALANCE_USD:
            return False, f"Balance ${b:.2f} < {MIN_BALANCE_USD}"
        return True, "OK"

    def execute(self, arb: dict, size: float) -> Optional[dict]:
        ok, reason = self.ok()
        if not ok:
            logger.warning(f"⛔ Risk block: {reason}")
            return None

        mkt = arb["market"]
        sz = min(size, arb["available_size"])

        if self.dry_run:
            logger.info(
                f"🔍 DRY: {sz:.1f}@{arb['ask_up']:.4f} UP + "
                f"{sz:.1f}@{arb['ask_down']:.4f} DOWN = "
                f"${sz*(arb['ask_up']+arb['ask_down']):.2f} | "
                f"+${arb['net_profit_pct']*sz:.2f} | {mkt['question'][:50]}"
            )
            return {"dry_run": True}

        logger.info(f"⚡ LIVE ARB: {sz:.1f}/side | +${arb['net_profit_pct']*sz:.2f}")
        try:
            r_up = self.client.create_and_post_order(
                OrderArgs(price=arb["ask_up"], size=sz, side=BUY, token_id=mkt["token_up"]),
                OrderType.GTC,
            )
            r_down = self.client.create_and_post_order(
                OrderArgs(price=arb["ask_down"], size=sz, side=BUY, token_id=mkt["token_down"]),
                OrderType.GTC,
            )
            rec = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "slug": mkt["slug"], "q": mkt["question"][:60],
                "sz": sz, "ask_up": arb["ask_up"], "ask_down": arb["ask_down"],
                "cost": sz*(arb["ask_up"]+arb["ask_down"]),
                "est_pnl": arb["net_profit_pct"]*sz,
                "up_ok": r_up is not None, "down_ok": r_down is not None,
            }
            self.history.append(rec)
            self._save()
            logger.info(f"✅ Done: UP={'OK' if r_up else 'FAIL'} DOWN={'OK' if r_down else 'FAIL'}")
            return rec
        except Exception as e:
            logger.error(f"❌ Execute failed: {e}")
            return None


# ============================================================
class MicroArbBot:
    def __init__(self, live: bool = False, trade_size: float = DEFAULT_TRADE_SIZE):
        load_dotenv(dotenv_path=BASE_DIR / ".env")
        self.live = live
        self.trade_size = trade_size
        self.scanner = MarketScanner()
        self.running = True

        pk = os.environ.get("POLY_PRIVATE_KEY")
        ak = os.environ.get("POLY_API_KEY", "")
        a_s = os.environ.get("POLY_API_SECRET", "")
        ap = os.environ.get("POLY_API_PASSPHRASE", "")
        dep = os.environ.get("POLY_DEPOSIT_WALLET", "")
        prx = os.environ.get("POLY_PROXY_WALLET", "")

        creds = ApiCreds(api_key=ak, api_secret=a_s, api_passphrase=ap)
        self.clob = ClobClient(
            host=POLYMARKET_CLOB, chain_id=137,
            key=pk, creds=creds, funder=prx or dep, signature_type=3,
        )
        self.detector = ArbDetector(self.clob)
        self.executor = ArbExecutor(self.clob, dry_run=not live)

        self.cycles = self.arbs_found = self.arbs_done = 0
        self.est_pnl = 0.0
        signal.signal(signal.SIGINT, lambda *a: setattr(self, 'running', False))

    def run(self, interval: float = 2.0):
        mode = "🚀 LIVE" if self.live else "🔍 DRY RUN"
        bal = self.executor.balance()
        logger.info(f"{'='*60}")
        logger.info(f"⚡ Micro Arb Bot — {mode}")
        logger.info(f"   BTC/ETH 5-min | threshold={ARB_THRESHOLD} | size=${self.trade_size}")
        logger.info(f"   Balance=${bal:.2f}")
        logger.info(f"{'='*60}")

        while self.running:
            try:
                self.cycles += 1
                markets = self.scanner.scan()
                if not markets:
                    time.sleep(interval); continue

                for mkt in markets:
                    arb = self.detector.check(mkt)
                    if not arb: continue

                    self.arbs_found += 1
                    logger.info(
                        f"🚨 #{self.arbs_found} | {mkt['question'][:50]} | "
                        f"UP={arb['ask_up']:.4f} DN={arb['ask_down']:.4f} "
                        f"SUM={arb['total_cost']:.4f} | +${arb['est_profit']:.2f}"
                    )
                    r = self.executor.execute(arb, self.trade_size)
                    if r:
                        self.arbs_done += 1
                        self.est_pnl += arb["est_profit"]

                if self.cycles % 20 == 0:
                    bal = self.executor.balance()
                    logger.info(
                        f"📊 C{self.cycles} | found={self.arbs_found} "
                        f"done={self.arbs_done} | est P&L=${self.est_pnl:.2f} | bal=${bal:.2f}"
                    )

                time.sleep(interval)
            except KeyboardInterrupt: break
            except Exception as e:
                logger.error(f"Cycle err: {e}")
                time.sleep(interval * 2)

        logger.info(f"Stopped. {self.arbs_found} found, {self.arbs_done} done.")


def main():
    p = argparse.ArgumentParser(description="Micro Arb Bot")
    p.add_argument("--live", action="store_true")
    p.add_argument("--size", type=float, default=DEFAULT_TRADE_SIZE)
    p.add_argument("--interval", type=float, default=2.0)
    a = p.parse_args()
    MicroArbBot(live=a.live, trade_size=a.size).run(interval=a.interval)

if __name__ == "__main__":
    main()
