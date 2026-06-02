"""Polymarket Integration - Search BTC 5-min markets and compare odds"""

import json
import logging
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import Optional
from config import *

logger = logging.getLogger(__name__)


class PolymarketClient:
    """Client for Polymarket BTC 5-minute Up/Down markets."""

    def __init__(self):
        self._market_cache = {}
        self._cache_time = 0
        self._cache_ttl = 60  # 1 min cache for fast-moving markets

    def _request(self, url: str) -> Optional[dict | list]:
        """Make HTTP GET request."""
        headers = {"User-Agent": "BTC-Predictor/1.0"}
        req = urllib.request.Request(url, headers=headers)
        try:
            if HTTP_PROXY:
                proxy = urllib.request.ProxyHandler({"https": HTTP_PROXY, "http": HTTP_PROXY})
                opener = urllib.request.build_opener(proxy)
            else:
                opener = urllib.request.build_opener()
            with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            logger.warning(f"Polymarket API error: {e}")
            return None

    def find_active_btc_5m_markets(self) -> list:
        """Find all active BTC Up/Down 5-minute markets.

        These markets are extremely short-lived (5-min windows).
        Strategy: compute expected slug from current time and query directly.
        """
        now = time.time()
        if self._market_cache and now - self._cache_time < self._cache_ttl:
            return self._market_cache.get("markets", [])

        from datetime import datetime, timezone, timedelta
        now_dt = datetime.now(timezone.utc)
        # Round to nearest 5-min window
        current_5min = now_dt.replace(second=0, microsecond=0)
        current_5min = current_5min.replace(minute=(current_5min.minute // 5) * 5)

        markets = []
        seen = set()

        # Try current window and nearby windows (past 2, current, next 2)
        for offset in range(-10, 15, 5):
            window_end = current_5min + timedelta(minutes=offset)
            ts = int(window_end.timestamp())
            slug = f"btc-updown-5m-{ts}"
            if slug in seen:
                continue
            seen.add(slug)
            try:
                m = self.get_market_by_slug(slug)
                if m and m.get("active") and not m.get("closed"):
                    markets.append({
                        "event_title": m["question"],
                        "question": m["question"],
                        "outcomes": m.get("outcomes", ["Up", "Down"]),
                        "yes_price": m["up_price"],
                        "no_price": m["down_price"],
                        "volume": m["volume"],
                        "liquidity": m["liquidity"],
                        "condition_id": m["condition_id"],
                        "token_ids": m["token_ids"],
                        "end_date": m["end_date"],
                        "slug": slug,
                    })
            except:
                pass

        markets.sort(key=lambda x: x.get("end_date", ""))
        self._market_cache = {"markets": markets, "time": now}
        self._cache_time = now
        return markets

    def get_market_by_slug(self, slug: str) -> Optional[dict]:
        """Get specific market by slug."""
        data = self._request(f"{POLYMARKET_GAMMA}/markets?slug={urllib.parse.quote(slug)}")
        if data and isinstance(data, list) and len(data) > 0:
            m = data[0]
            prices = json.loads(m.get("outcomePrices", "[]"))
            tokens = json.loads(m.get("clobTokenIds", "[]"))
            outcomes = json.loads(m.get("outcomes", "[]"))

            return {
                "question": m.get("question", ""),
                "outcomes": outcomes if isinstance(outcomes, list) else ["Up", "Down"],
                "up_price": float(prices[0]) if isinstance(prices, list) and len(prices) >= 2 else 0.5,
                "down_price": float(prices[1]) if isinstance(prices, list) and len(prices) >= 2 else 0.5,
                "volume": float(m.get("volume", 0)),
                "liquidity": float(m.get("liquidity", 0)),
                "condition_id": m.get("conditionId", ""),
                "token_ids": tokens if isinstance(tokens, list) else [],
                "end_date": m.get("endDate", ""),
                "active": m.get("active", False),
                "closed": m.get("closed", False),
            }
        return None

    def get_clob_price(self, token_id: str) -> Optional[float]:
        """Get CLOB midpoint price for a token."""
        data = self._request(f"{POLYMARKET_CLOB}/midpoint?token_id={token_id}")
        if data and "mid" in data:
            return float(data["mid"])
        return None

    def get_orderbook(self, token_id: str) -> Optional[dict]:
        """Get orderbook for a token."""
        data = self._request(f"{POLYMARKET_CLOB}/book?token_id={token_id}")
        if data:
            return {
                "bids": data.get("bids", []),
                "asks": data.get("asks", []),
                "last_trade": data.get("last_trade_price", "0"),
                "min_order_size": data.get("min_order_size", "5"),
                "tick_size": data.get("tick_size", "0.01"),
            }
        return None

    def get_best_market(self) -> Optional[dict]:
        """Get the next upcoming active BTC 5-min market."""
        markets = self.find_active_btc_5m_markets()
        if markets:
            return markets[0]  # Earliest ending first
        return None


def compare_prediction_vs_market(prediction: dict, market: dict) -> dict:
    """Compare prediction confidence vs Polymarket odds.

    The market resolves "Up" if BTC price at end >= price at start of 5-min window.
    Resolution via Chainlink BTC/USD data stream.

    Args:
        prediction: {"direction": 1/-1, "confidence": 0-1}
        market: {"up_price": float, "down_price": float, ...}

    Returns:
        Edge analysis dict
    """
    pred_direction = prediction["direction"]  # 1 = UP, -1 = DOWN
    pred_confidence = prediction["confidence"]

    up_price = market.get("up_price", 0.5)
    down_price = market.get("down_price", 0.5)

    if pred_direction == 1:
        # Predicting UP → compare our confidence vs market's Up price
        our_prob = pred_confidence
        market_prob = up_price
        edge = our_prob - market_prob
        action = "BUY_UP" if edge > EDGE_THRESHOLD else "HOLD"
        bet_on = "Up"
    else:
        # Predicting DOWN → compare our confidence vs market's Down price
        our_prob = pred_confidence
        market_prob = down_price
        edge = our_prob - market_prob
        action = "BUY_DOWN" if edge > EDGE_THRESHOLD else "HOLD"
        bet_on = "Down"

    # Expected value calculation
    # If we buy at market_prob and win, we get $1 per share
    # Expected value = edge * 1 - (1 - our_prob) * cost
    ev_per_share = edge  # simplified EV

    return {
        "prediction_direction": "UP" if pred_direction == 1 else "DOWN",
        "prediction_confidence": float(pred_confidence),
        "market_up_price": float(up_price),
        "market_down_price": float(down_price),
        "our_prob_up": float(pred_confidence) if pred_direction == 1 else float(1 - pred_confidence),
        "our_prob_down": float(1 - pred_confidence) if pred_direction == 1 else float(pred_confidence),
        "edge": float(edge),
        "ev_per_share": float(ev_per_share),
        "action": action,
        "bet_on": bet_on,
        "recommendation": (
            f"✅ Edge found! {action} at {market_prob:.3f} "
            f"(our prob: {our_prob:.3f})"
            if action != "HOLD"
            else "⚠️ No significant edge — HOLD"
        ),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
