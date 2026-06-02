"""
🎯 Copy Trader — Mirror @0xcE25E214...E7Fdc's Polymarket Positions

Monitors the $175K PnL trader's positions in real-time and copies new entries
with proportional sizing.

Usage:
  python3 copy_trader.py              # Dry run
  python3 copy_trader.py --live       # Live copy-trading
  python3 copy_trader.py --live --max-size 20  # Cap at $20 per trade
"""

import os, sys, json, time, signal, logging, argparse, subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, List, Set, Tuple

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

TARGET = "0xce25e214d5cfe4f459cf67f08df581885aae7fdc"
DATA_API = "https://data-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
POLL_INTERVAL = 3
MIN_TRADE_SIZE = 5.0
DEFAULT_MAX_SIZE = 15.0
TARGET_PORTFOLIO = 175_000  # approximate, updated live

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "copy_trader.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("copy_trader")


def curl_json(url: str) -> Optional[dict]:
    try:
        r = subprocess.run(
            ["curl", "-s", "--connect-timeout", "3", "--max-time", "6", url],
            capture_output=True, text=True, timeout=8,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return json.loads(r.stdout)
    except:
        return None


class CopyTrader:
    def __init__(self, live: bool = False, max_size: float = DEFAULT_MAX_SIZE):
        self.live = live
        self.max_size = max_size
        self.seen_positions: Set[str] = set()  # "conditionId:outcome"
        self.open_copies: Dict[str, dict] = {}  # our copies
        self.running = True

        # Init CLOB client
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=BASE_DIR / ".env")
        pk = os.environ.get("POLY_PRIVATE_KEY")
        ak = os.environ.get("POLY_API_KEY", "")
        a_s = os.environ.get("POLY_API_SECRET", "")
        ap = os.environ.get("POLY_API_PASSPHRASE", "")
        dep = os.environ.get("POLY_DEPOSIT_WALLET", "")
        prx = os.environ.get("POLY_PROXY_WALLET", "")

        creds = ApiCreds(api_key=ak, api_secret=a_s, api_passphrase=ap)
        self.clob = ClobClient(
            host=CLOB_HOST, chain_id=137,
            key=pk, creds=creds, funder=prx or dep, signature_type=3,
        )

        self._load_state()
        signal.signal(signal.SIGINT, lambda *a: setattr(self, 'running', False))

    def _load_state(self):
        sf = DATA_DIR / "copy_state.json"
        if sf.exists():
            try:
                s = json.loads(sf.read_text())
                self.seen_positions = set(s.get("seen", []))
                self.open_copies = s.get("open", {})
                logger.info(f"Loaded state: {len(self.seen_positions)} seen, {len(self.open_copies)} open copies")
            except: pass

    def _save_state(self):
        sf = DATA_DIR / "copy_state.json"
        sf.write_text(json.dumps({
            "seen": list(self.seen_positions),
            "open": self.open_copies,
            "updated": datetime.now(timezone.utc).isoformat(),
        }, indent=2, default=str))

    def balance(self) -> float:
        try:
            info = self.clob.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            return float(info.get("balance", 0)) / 1e6
        except: return 0.0

    def get_target_positions(self) -> List[dict]:
        """Fetch target's current positions."""
        return curl_json(
            f"{DATA_API}/positions?user={TARGET}"
            f"&sortBy=CURRENT&sortDirection=DESC&sizeThreshold=.1&limit=50"
        ) or []

    def get_target_portfolio(self) -> float:
        """Get target's total portfolio value."""
        data = curl_json(f"{DATA_API}/value?user={TARGET}")
        if data and isinstance(data, list) and data:
            return float(data[0].get("value", TARGET_PORTFOLIO))
        return TARGET_PORTFOLIO

    def position_key(self, pos: dict) -> str:
        """Unique key for a position."""
        return f"{pos.get('conditionId','')}:{pos.get('outcome','')}"

    def calculate_size(self, their_value: float) -> float:
        """Calculate our trade size proportional to target's position."""
        our_bal = self.balance()
        their_portfolio = self.get_target_portfolio()

        if their_portfolio <= 0:
            return MIN_TRADE_SIZE

        # Proportional: our size = their position value × (our balance / their portfolio)
        ratio = our_bal / their_portfolio
        size = their_value * ratio

        # Clamp between min and max
        size = max(MIN_TRADE_SIZE, min(size, self.max_size))
        return round(size, 2)

    def buy_token(self, asset_id: str, price: float, size: float, pos: dict) -> Optional[dict]:
        """Buy a specific token."""
        if self.live:
            try:
                from py_clob_client_v2.clob_types import PartialCreateOrderOptions
                result = self.clob.create_and_post_order(
                    OrderArgs(price=price, size=size, side=BUY, token_id=asset_id),
                    options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
                    order_type=OrderType.GTC,
                )
                return {"ok": True, "result": str(result)[:100]} if result else {"ok": False}
            except Exception as e:
                logger.error(f"Buy failed: {e}")
                return {"ok": False, "error": str(e)}
        else:
            logger.info(f"🔍 DRY: BUY {size:.1f} @ ${price:.4f} | {pos.get('title','')[:50]} {pos.get('outcome','')}")
            return {"ok": True, "dry": True}

    def scan_and_copy(self):
        """One scan cycle."""
        positions = self.get_target_positions()
        if not positions:
            return

        their_portfolio = self.get_target_portfolio()
        our_bal = self.balance()
        new_positions = 0
        copied = 0
        
        # Balance guard: don't copy if we have less than 2x min trade
        if our_bal < MIN_TRADE_SIZE * 2:
            return

        for pos in positions:
            key = self.position_key(pos)
            if key in self.seen_positions:
                continue
            
            their_value = float(pos.get("currentValue", 0))
            
            # Skip dust positions (target might have tiny leftover shares)
            if their_value < 1.0:
                self.seen_positions.add(key)  # still mark as seen
                continue

            # Balance check: stop copying if we're running low
            if our_bal < MIN_TRADE_SIZE:
                logger.info(f"⛔ Balance ${our_bal:.2f} < min ${MIN_TRADE_SIZE}, pausing copy")
                break

            # NEW meaningful position detected!
            self.seen_positions.add(key)
            new_positions += 1

            title = pos.get("title", "?")
            outcome = pos.get("outcome", "?")
            asset_id = pos.get("asset", "")
            cur_price = float(pos.get("curPrice", 0.5))
            their_size = float(pos.get("size", 0))
            avg_price = float(pos.get("avgPrice", 0))

            # Calculate our size
            our_size = self.calculate_size(their_value)

            logger.info(
                f"🎯 COPY | {title[:50]} | {outcome} | "
                f"their=${their_value:.2f} | "
                f"our=${our_size:.1f} @ ${cur_price:.4f} | "
                f"bal=${our_bal:.2f}"
            )

            # Execute copy
            result = self.buy_token(asset_id, cur_price, our_size, pos)
            if result and result.get("ok"):
                copied += 1
                our_bal -= our_size  # track locally
                self.open_copies[key] = {
                    "copied_at": datetime.now(timezone.utc).isoformat(),
                    "asset": asset_id, "size": our_size,
                    "price": cur_price, "title": title, "outcome": outcome,
                }
            
            self._save_state()  # save after every position (success or fail)

        if new_positions > 0:
            logger.info(f"📊 {new_positions} new, {copied} copied | bal=${self.balance():.2f}")

    def run(self):
        mode = "🚀 LIVE" if self.live else "🔍 DRY RUN"
        bal = self.balance()
        their_val = self.get_target_portfolio()

        logger.info("=" * 60)
        logger.info(f"🎯 Copy Trader — {mode}")
        logger.info(f"   Target: polymarket.com/profile/{TARGET[:10]}...")
        logger.info(f"   Their portfolio: ${their_val:,.0f}")
        logger.info(f"   Our balance: ${bal:.2f}")
        logger.info(f"   Copy ratio: {bal/their_val*100:.4f}%")
        logger.info(f"   Max trade: ${self.max_size}")
        logger.info("=" * 60)

        while self.running:
            try:
                self.scan_and_copy()
                time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt: break
            except Exception as e:
                logger.error(f"Cycle error: {e}")
                time.sleep(POLL_INTERVAL * 2)

        self._save_state()
        logger.info("Stopped.")


def main():
    p = argparse.ArgumentParser(description="Copy Trader")
    p.add_argument("--live", action="store_true")
    p.add_argument("--max-size", type=float, default=DEFAULT_MAX_SIZE)
    args = p.parse_args()
    CopyTrader(live=args.live, max_size=args.max_size).run()


if __name__ == "__main__":
    main()
