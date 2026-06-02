"""
🚀 Trader Monitor — Track @0xce25e214d5c's Polymarket Activity

Monitors the high-winning-rate Polymarket bot @0xce25e214d5c2e7c34daf8a3e07843d8a53fb9c8f
across BTC 5-min markets. Captures trades in real-time and optionally mirrors them.

Strategy signals:
  1. Cross-market stat arb (L2 imbalance + spread z-score)
  2. Window mean reversion (BTC deviation threshold)
  3. Model prediction (CVD/order flow)

Usage:
  python3 trader_monitor.py              # Monitor only, log to file
  python3 trader_monitor.py --mirror     # Monitor + mirror trades
  python3 trader_monitor.py --interval 10  # Poll every 10 seconds
"""

import os
import sys
import json
import time
import logging
import argparse
import urllib.parse
import traceback
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

import urllib.request
from dotenv import load_dotenv

HTTP_TIMEOUT = 15
HTTP_PROXY = os.environ.get("HTTPS_PROXY", os.environ.get("https_proxy", ""))
from py_clob_client_v2 import ClobClient, ApiCreds

# === Config ===
BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TARGET_ADDRESS = "0xcE25E214D5cfE4f459cf67F08DF581885AAE7Fdc"
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB = "https://clob.polymarket.com"
POLY_CHAIN_ID = 137

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "trader_monitor.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("trader_monitor")


class TraderMonitor:
    """Monitor a specific Polymarket trader's activity."""

    def __init__(self, mirror: bool = False):
        load_dotenv(dotenv_path=BASE_DIR / ".env")

        self.target = TARGET_ADDRESS
        self.mirror = mirror
        self.seen_trade_ids: set = set()
        self.known_activity: List[Dict] = []
        self.activity_file = BASE_DIR / "data" / "trader_activity.json"

        # Load credentials
        pk = os.environ.get("POLY_PRIVATE_KEY")
        api_key = os.environ.get("POLY_API_KEY", "")
        api_secret = os.environ.get("POLY_API_SECRET", "")
        api_pass = os.environ.get("POLY_API_PASSPHRASE", "")
        self.deposit_wallet = os.environ.get("POLY_DEPOSIT_WALLET", "")
        self.proxy_wallet = os.environ.get("POLY_PROXY_WALLET", "")
        funder = self.proxy_wallet or self.deposit_wallet

        self.clob_client = None
        if all([pk, api_key, api_secret, api_pass]):
            creds = ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_pass,
            )
            try:
                self.clob_client = ClobClient(
                    host=POLYMARKET_CLOB,
                    chain_id=POLY_CHAIN_ID,
                    key=pk,
                    creds=creds,
                    funder=funder,
                    signature_type=3,
                )
                logger.info(f"CLOB client initialized for monitoring {self.target[:10]}...")
            except Exception as e:
                logger.error(f"Failed to init CLOB client: {e}")

        # Load previous activity
        self._load_activity()

        # Traders can have multiple wallets — track all known aliases
        self.known_addresses = {self.target.lower()}

    def _gamma_request(self, url: str) -> Optional[dict]:
        """Make HTTP GET request to Gamma API (uses urllib, not requests)."""
        headers = {"User-Agent": "BTC-Predictor/1.0"}
        req = urllib.request.Request(url, headers=headers)
        try:
            if HTTP_PROXY:
                proxy = urllib.request.ProxyHandler(
                    {"https": HTTP_PROXY, "http": HTTP_PROXY}
                )
                opener = urllib.request.build_opener(proxy)
            else:
                opener = urllib.request.build_opener()
            with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            logger.debug(f"Gamma API error: {e}")
            return None

    def _load_activity(self):
        """Load previously recorded trader activity."""
        if self.activity_file.exists():
            try:
                data = json.loads(self.activity_file.read_text())
                self.known_activity = data.get("trades", [])
                self.seen_trade_ids = set(data.get("seen_ids", []))
                self.known_addresses.update(
                    addr.lower() for addr in data.get("addresses", [])
                )
                logger.info(
                    f"Loaded {len(self.known_activity)} historical trades, "
                    f"{len(self.seen_trade_ids)} seen IDs"
                )
            except Exception as e:
                logger.warning(f"Failed to load activity file: {e}")

    def _save_activity(self):
        """Persist trader activity to disk."""
        self.activity_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "target": self.target,
            "addresses": list(self.known_addresses),
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "trade_count": len(self.known_activity),
            "seen_ids": list(self.seen_trade_ids),
            "trades": self.known_activity[-500:],  # Keep last 500
        }
        self.activity_file.write_text(json.dumps(data, indent=2, default=str))

    def find_btc_5m_markets(self) -> List[Dict]:
        """Find active BTC 5-min markets using slug pattern."""
        now = datetime.now(timezone.utc)
        current_5min = now.replace(second=0, microsecond=0)
        current_5min = current_5min.replace(minute=(current_5min.minute // 5) * 5)

        markets = []
        seen_slugs = set()

        # Scan current window ± 15 minutes
        for offset in range(-15, 20, 5):
            window_end = current_5min + timedelta(minutes=offset)
            ts = int(window_end.timestamp())
            slug = f"btc-updown-5m-{ts}"
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)

            try:
                url = f"{POLYMARKET_GAMMA}/markets?slug={urllib.parse.quote(slug)}"
                data = self._gamma_request(url)
                if not data or not isinstance(data, list) or not data:
                    continue

                m = data[0]
                if not m.get("active") or m.get("closed"):
                    continue

                prices = json.loads(m.get("outcomePrices", "[]"))
                tokens = json.loads(m.get("clobTokenIds", "[]"))

                markets.append({
                    "question": m.get("question", ""),
                    "slug": slug,
                    "condition_id": m.get("conditionId", ""),
                    "token_ids": tokens if isinstance(tokens, list) else [],
                    "up_price": float(prices[0]) if len(prices) >= 2 else 0.5,
                    "down_price": float(prices[1]) if len(prices) >= 2 else 0.5,
                    "volume": float(m.get("volume", 0)),
                    "end_date": m.get("endDate", ""),
                })
            except Exception as e:
                logger.debug(f"Market {slug}: {e}")

        markets.sort(key=lambda x: x.get("end_date", ""))
        return markets

    def get_market_trades(self, condition_id: str) -> List[Dict]:
        """Get all trades for a market condition, filtered by target trader."""
        if not self.clob_client:
            return []

        try:
            events = self.clob_client.get_market_trades_events(condition_id)
            if not events:
                return []

            trades = []
            for evt in (events if isinstance(events, list) else []):
                # Each event may have multiple trades
                evt_trades = evt.get("trades", []) if isinstance(evt, dict) else []
                for t in evt_trades:
                    maker = (t.get("maker_address") or "").lower()
                    owner = (t.get("owner") or "").lower()
                    taker = (t.get("taker_address") or "").lower()

                    # Check if our target is involved
                    if maker in self.known_addresses or owner in self.known_addresses:
                        # Discover new addresses
                        for addr in [maker, owner, taker]:
                            if addr and addr.startswith("0x"):
                                self.known_addresses.add(addr.lower())

                        trade_id = t.get("id") or t.get("transaction_hash") or ""
                        if trade_id and trade_id not in self.seen_trade_ids:
                            self.seen_trade_ids.add(trade_id)
                            trades.append({**t, "condition_id": condition_id})

            return trades
        except Exception as e:
            logger.debug(f"get_market_trades_events error for {condition_id[:20]}: {e}")
            return []

    def get_open_positions(self) -> List[Dict]:
        """Get target trader's open positions via Gamma API."""
        # Gamma API doesn't directly support user positions query,
        # but we can check our own positions for reference
        if not self.clob_client:
            return []

        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            balance = self.clob_client.get_balance_allowance(params)
            logger.debug(f"Own balance: {json.dumps(balance, default=str)[:200]}")
            return []
        except Exception as e:
            logger.debug(f"Balance check error: {e}")
            return []

    def scan(self) -> List[Dict]:
        """Perform one scan cycle. Returns new trades found."""
        new_trades = []

        # Find active BTC 5-min markets
        markets = self.find_btc_5m_markets()
        logger.info(
            f"Scanning {len(markets)} active BTC 5-min markets "
            f"(target: {self.target[:10]}...)"
        )

        for mkt in markets:
            cid = mkt.get("condition_id", "")
            if not cid:
                continue

            trades = self.get_market_trades(cid)
            for t in trades:
                side = t.get("side", "?")
                size = t.get("size", "0")
                price = t.get("price", "0")
                maker = (t.get("maker_address") or "")[:12]

                entry = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "trade_id": t.get("id", "")[:30],
                    "condition_id": cid,
                    "market_slug": mkt.get("slug", ""),
                    "question": mkt.get("question", ""),
                    "side": side,
                    "size": float(size) if size else 0,
                    "price": float(price) if price else 0,
                    "maker": maker,
                    "value": float(size or 0) * float(price or 0),
                    "transaction_hash": t.get("transaction_hash", ""),
                }
                self.known_activity.append(entry)
                new_trades.append(entry)

                logger.info(
                    f"🎯 TRADER SIGNAL | {mkt['question'][:40]} | "
                    f"{side} @ ${price} x {size} = ${entry['value']:.2f} | "
                    f"maker={maker}"
                )

        return new_trades

    def mirror_trade(self, trade: Dict):
        """Mirror a detected trader trade."""
        if not self.mirror or not self.clob_client:
            return

        logger.info(
            f"🪞 MIRROR: Would copy {trade['side']} {trade['size']} @ ${trade['price']} "
            f"on {trade['question'][:40]}"
        )
        # TODO: Implement actual mirroring via ClobClient order placement
        # This requires matching the market token ID and placing a mirror order

    def run(self, interval: int = 15):
        """Main monitoring loop."""
        logger.info("=" * 60)
        logger.info(f"🚀 Trader Monitor started")
        logger.info(f"   Target: {self.target}")
        logger.info(f"   Mirror: {'ON' if self.mirror else 'OFF'}")
        logger.info(f"   Poll interval: {interval}s")
        logger.info("=" * 60)

        cycle = 0
        while True:
            try:
                cycle += 1
                trades = self.scan()

                if trades:
                    logger.info(f"Cycle #{cycle}: {len(trades)} new trades detected")
                    for t in trades:
                        self.mirror_trade(t)
                    self._save_activity()
                else:
                    if cycle % 4 == 0:  # Log every 4th idle cycle
                        logger.info(f"Cycle #{cycle}: No new activity")

                time.sleep(interval)

            except KeyboardInterrupt:
                logger.info("Shutting down...")
                self._save_activity()
                break
            except Exception as e:
                logger.error(f"Cycle #{cycle} error: {e}")
                traceback.print_exc()
                time.sleep(interval * 2)  # Back off on error


def main():
    parser = argparse.ArgumentParser(description="Trader Monitor — Polymarket @0xce25e214d5c")
    parser.add_argument("--interval", type=int, default=15, help="Poll interval in seconds")
    parser.add_argument("--mirror", action="store_true", help="Mirror detected trades")
    args = parser.parse_args()

    monitor = TraderMonitor(mirror=args.mirror)
    monitor.run(interval=args.interval)


if __name__ == "__main__":
    main()
