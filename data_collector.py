"""BTC Price Data Collector - Multi-source with Chainlink support"""

import json
import time
import logging
import urllib.request
import urllib.error
import ssl
from datetime import datetime, timezone
from typing import Optional
from config import *

logger = logging.getLogger(__name__)

_ssl_context = ssl._create_unverified_context()


def _request(url: str, timeout: int = HTTP_TIMEOUT) -> Optional[dict]:
    """Make HTTP GET request."""
    headers = {"User-Agent": "BTC-Predictor/1.0"}
    req = urllib.request.Request(url, headers=headers)
    try:
        if HTTP_PROXY:
            proxy = urllib.request.ProxyHandler({"https": HTTP_PROXY, "http": HTTP_PROXY})
            opener = urllib.request.build_opener(proxy)
        else:
            opener = urllib.request.build_opener()
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning(f"Request failed: {url} — {e}")
        return None


class BinanceCollector:
    """BTC data from Binance."""

    BASE = BINANCE_API

    def get_price(self) -> Optional[float]:
        data = _request(f"{self.BASE}/api/v3/ticker/price?symbol=BTCUSDT")
        return float(data["price"]) if data else None

    def get_klines(self, interval: str = "1m", limit: int = 100) -> Optional[list]:
        data = _request(f"{self.BASE}/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}")
        if not data:
            return None
        return [{
            "timestamp": k[0] / 1000,
            "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
            "close": float(k[4]), "volume": float(k[5]),
            "close_time": k[6] / 1000, "quote_volume": float(k[7]),
            "trades": int(k[8]),
        } for k in data]


class OKXCollector:
    """BTC data from OKX (Chinese exchange)."""

    BASE = OKX_API

    def get_price(self) -> Optional[float]:
        data = _request(f"{self.BASE}/api/v5/market/ticker?instId=BTC-USDT")
        if data and data.get("code") == "0" and data.get("data"):
            return float(data["data"][0]["last"])
        return None

    def get_klines(self, bar: str = "1m", limit: int = 100) -> Optional[list]:
        data = _request(f"{self.BASE}/api/v5/market/candles?instId=BTC-USDT&bar={bar}&limit={limit}")
        if not data or data.get("code") != "0":
            return None
        return [{
            "timestamp": int(k[0]) / 1000,
            "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
            "close": float(k[4]), "volume": float(k[5]),
            "close_time": int(k[0]) / 1000, "quote_volume": float(k[7]),
            "trades": 0,
        } for k in data.get("data", [])]


class ChainlinkCollector:
    """BTC/USD price from Chainlink data feed (the actual resolution source for Polymarket).

    Chainlink BTC/USD stream: https://data.chain.link/streams/btc-usd
    We use their public API to get the latest round data.
    """

    # Chainlink feeds API
    FEEDS_URL = "https://data.chain.link/graphql"

    def get_latest_price(self) -> Optional[float]:
        """Get latest BTC/USD price from Chainlink."""
        # Chainlink has a REST endpoint for latest price
        data = _request("https://api.chain.link/latest?chain=ethereum&pair=BTC-USD")
        if data and "data" in data:
            # Chainlink returns price with 8 decimals
            price_raw = data["data"]["price"]
            # The price is in the format with 8 decimal places
            return float(price_raw) / 1e8
        return None

    def get_price_history(self, hours: int = 1) -> Optional[list]:
        """Get recent price history from Chainlink."""
        # Try the subgraph API
        data = _request(
            f"https://api.chain.link/feeds/ethereum/btc-usd?hours={hours}"
        )
        if data and "data" in data:
            return data["data"]
        return None


class DataCollector:
    """Multi-source BTC data collector with fallback chain."""

    def __init__(self):
        self.binance = BinanceCollector()
        self.okx = OKXCollector()
        self.chainlink = ChainlinkCollector()
        self._last_price = None
        self._last_klines = None

    def get_price(self) -> Optional[float]:
        """Get current BTC price with fallback."""
        # Binance first (most liquid)
        price = self.binance.get_price()
        if price:
            logger.info(f"Binance price: ${price:,.2f}")
            self._last_price = price
            return price

        # Fallback to OKX
        price = self.okx.get_price()
        if price:
            logger.info(f"OKX price: ${price:,.2f}")
            self._last_price = price
            return price

        logger.error("All price sources failed!")
        return None

    def get_klines(self) -> Optional[list]:
        """Get 1-minute klines with fallback."""
        klines = self.binance.get_klines()
        if klines and len(klines) > 10:
            self._last_klines = klines
            return klines

        klines = self.okx.get_klines()
        if klines and len(klines) > 10:
            self._last_klines = klines
            return klines

        logger.error("All kline sources failed!")
        return None

    def get_chainlink_price(self) -> Optional[float]:
        """Get Chainlink BTC/USD price (resolution source for Polymarket)."""
        price = self.chainlink.get_latest_price()
        if price:
            logger.info(f"Chainlink price: ${price:,.2f}")
            return price
        logger.warning("Chainlink price unavailable, using Binance")
        return self.get_price()
