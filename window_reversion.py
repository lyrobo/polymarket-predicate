"""
Window Reversion Detector — User's 90%-winrate strategy
=========================================================

Strategy:
  BTC 5-min windows on Polymarket. When BTC drops a significant amount
  from window open but UP token is still ≥50¢, buy UP because BTC
  almost always mean-reverts before the window closes. Vice versa for DOWN.

  This exploits the lag between real BTC price movement and Polymarket
  odds adjustment — market makers don't update instantaneously.

  Key insight from user: "熟能生巧" (skill comes with practice) — the
  more you watch, the better you get at spotting the reversals.

Usage:
    detector = WindowReversionDetector()
    detector.attach_websocket(ws_client)

    # Each cycle:
    signal = detector.check(market, min_time_remaining=90)
    if signal['action'] in ('BUY_UP', 'BUY_DN'):
        # Place the trade at signal['confidence']
"""

import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict

logger = logging.getLogger(__name__)


class WindowReversionDetector:
    """Detect window-based mean reversion opportunities.

    Tracks BTC price at the start of each 5-minute window and fires
    signals when price deviates significantly while Polymarket odds
    haven't caught up — creating a high-probability reversal trade.
    """

    def __init__(
        self,
        drop_threshold: float = 30.0,    # $USD drop to trigger BUY_UP
        pump_threshold: float = 30.0,    # $USD pump to trigger BUY_DN
        min_odds: float = 0.50,          # minimum token price (must be ≥ this)
        min_time_remaining: int = 60,     # seconds remaining in window to trade
        max_time_from_start: int = 240,   # don't trade too early (need confirmation)
        min_confidence: float = 0.75,     # minimum signal confidence
    ):
        self.drop_threshold = drop_threshold
        self.pump_threshold = pump_threshold
        self.min_odds = min_odds
        self.min_time_remaining = min_time_remaining
        self.max_time_from_start = max_time_from_start
        self.min_confidence = min_confidence

        self.ws = None
        self.window_open_price: Optional[float] = None
        self.window_open_time: Optional[float] = None
        self.window_end_time: Optional[int] = None  # unix timestamp
        self.current_window_slugs: set = set()       # track processed windows

    def attach_websocket(self, ws_client):
        """Attach WebSocket client for real-time BTC mid price."""
        self.ws = ws_client
        logger.info("WindowReversionDetector: WebSocket attached")

    def _get_current_price(self) -> float:
        """Get current BTC mid price from WebSocket."""
        if self.ws is None:
            return 0.0
        return self.ws.order_book.get_mid_price()

    def _detect_window(self, market: Dict) -> tuple:
        """Detect current 5-min window from Polymarket market data.

        Returns (window_end_ts, window_start_ts) or (None, None).
        """
        slug = market.get('slug', '')
        if not slug or '-' not in slug:
            return None, None

        # Slug format: btc-updown-5m-<epoch>
        try:
            parts = slug.split('-')
            epoch = int(parts[-1])
        except (ValueError, IndexError):
            return None, None

        window_end_ts = epoch
        window_start_ts = window_end_ts - 300  # 5 minutes

        return window_end_ts, window_start_ts

    def _time_remaining(self) -> int:
        """Seconds remaining in current window."""
        if self.window_end_time is None:
            return 0
        return max(0, int(self.window_end_time - time.time()))

    def _time_from_start(self) -> int:
        """Seconds elapsed since window open."""
        if self.window_open_time is None:
            return 0
        return max(0, int(time.time() - self.window_open_time))

    def _update_window(self, market: Dict):
        """Track window open price when entering a new window."""
        window_end_ts, window_start_ts = self._detect_window(market)
        if window_end_ts is None:
            return

        # Check if we entered a new window
        if window_end_ts != self.window_end_time:
            self.window_end_time = window_end_ts
            # Get current price as proxy for window open price
            # (there may be a slight lag — acceptable)
            price = self._get_current_price()
            if price > 0:
                self.window_open_price = price
                self.window_open_time = time.time()
                logger.info(
                    f"📊 New window: end={datetime.fromtimestamp(window_end_ts, tz=timezone.utc).strftime('%H:%M:%S')} "
                    f"open_price=${price:,.2f}"
                )

    def check(self, market: Dict) -> Dict:
        """Check for window reversion signal.

        Args:
            market: Polymarket market data (from PolymarketEdgeFinder)

        Returns:
            dict with keys: action, confidence, reason, deviation, time_remaining
        """
        # Update window tracking
        self._update_window(market)

        now = time.time()

        # Must have a window open price
        if self.window_open_price is None or self.window_open_price <= 0:
            return {'action': 'HOLD', 'confidence': 0.0, 'reason': 'no window reference price',
                    'deviation': 0, 'deviation_pct': 0, 'time_remaining': 0,
                    'window_open_price': 0, 'current_price': 0,
                    'up_price': 0.5, 'down_price': 0.5}

        # Time checks
        elapsed = self._time_from_start()
        remaining = self._time_remaining()

        # Don't trade too early (need confirmation of the move)
        if elapsed < 30:
            return {'action': 'HOLD', 'confidence': 0.0,
                    'reason': f'too early ({elapsed}s elapsed, need ≥30s)',
                    'deviation': 0, 'deviation_pct': 0, 'time_remaining': remaining,
                    'window_open_price': self.window_open_price, 'current_price': 0,
                    'up_price': 0.5, 'down_price': 0.5}

        # Don't trade too late (not enough time for reversion)
        if remaining < self.min_time_remaining:
            return {'action': 'HOLD', 'confidence': 0.0,
                    'reason': f'too late ({remaining}s remaining, need ≥{self.min_time_remaining}s)',
                    'deviation': 0, 'deviation_pct': 0, 'time_remaining': remaining,
                    'window_open_price': self.window_open_price, 'current_price': 0,
                    'up_price': 0.5, 'down_price': 0.5}

        # Must be before max_time_from_start
        if elapsed > self.max_time_from_start:
            return {'action': 'HOLD', 'confidence': 0.0,
                    'reason': f'too far into window ({elapsed}s, max {self.max_time_from_start}s)',
                    'deviation': 0, 'deviation_pct': 0, 'time_remaining': remaining,
                    'window_open_price': self.window_open_price, 'current_price': 0,
                    'up_price': 0.5, 'down_price': 0.5}

        # Get current price and deviation
        current_price = self._get_current_price()
        if current_price <= 0:
            return {'action': 'HOLD', 'confidence': 0.0, 'reason': 'no current price',
                    'deviation': 0, 'deviation_pct': 0, 'time_remaining': remaining,
                    'window_open_price': self.window_open_price, 'current_price': 0,
                    'up_price': 0.5, 'down_price': 0.5}

        price_change = current_price - self.window_open_price
        deviation_pct = (price_change / self.window_open_price) * 100

        # Get Polymarket odds
        up_price = market.get('up_price', 0.5)
        down_price = market.get('down_price', 0.5)

        result = {
            'action': 'HOLD',
            'confidence': 0.0,
            'reason': '',
            'deviation': price_change,
            'deviation_pct': deviation_pct,
            'time_remaining': remaining,
            'window_open_price': self.window_open_price,
            'current_price': current_price,
            'up_price': up_price,
            'down_price': down_price,
        }

        # --- Signal: BTC dropped → buy UP if odds still ≥ min_odds ---
        if price_change <= -self.drop_threshold:
            # BTC dropped $X. Check UP token price.
            if up_price >= self.min_odds:
                # The stronger the drop relative to the odds discount, the more confident
                confidence = min(0.95, 0.60 + (abs(price_change) / self.drop_threshold - 1) * 0.15)
                confidence = min(confidence, 0.95)  # cap at 95%

                # Extra boost: if UP still ≥0.50 despite big drop → market lag = edge
                if up_price >= 0.50:
                    confidence += 0.05

                if elapsed >= 60:
                    confidence += 0.03  # sustained move = more reliable

                confidence = min(confidence, 0.95)

                if confidence >= self.min_confidence:
                    result['action'] = 'BUY_UP'
                    result['confidence'] = round(confidence, 4)
                    result['reason'] = (
                        f'BTC dropped ${abs(price_change):.0f} ({deviation_pct:.2f}%), '
                        f'UP still {up_price:.3f} — mean reversion expected'
                    )

        # --- Signal: BTC pumped → buy DOWN if odds still ≥ min_odds ---
        elif price_change >= self.pump_threshold:
            if down_price >= self.min_odds:
                confidence = min(0.95, 0.60 + (price_change / self.pump_threshold - 1) * 0.15)
                confidence = min(confidence, 0.95)

                if down_price >= 0.50:
                    confidence += 0.05

                if elapsed >= 60:
                    confidence += 0.03

                confidence = min(confidence, 0.95)

                if confidence >= self.min_confidence:
                    result['action'] = 'BUY_DN'
                    result['confidence'] = round(confidence, 4)
                    result['reason'] = (
                        f'BTC pumped ${price_change:.0f} ({deviation_pct:.2f}%), '
                        f'DOWN still {down_price:.3f} — mean reversion expected'
                    )

        return result


# Quick test
if __name__ == '__main__':
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    from websocket_collector import WebSocketClient

    detector = WindowReversionDetector(
        drop_threshold=30.0,
        pump_threshold=30.0,
        min_odds=0.49,      # slightly lower to catch more
        min_time_remaining=90,
        max_time_from_start=240,
        min_confidence=0.70,
    )

    ws = WebSocketClient(symbol="BTC-USDT")
    detector.attach_websocket(ws)
    ws.start()

    print("Window Reversion Detector test — Ctrl+C to stop")
    print(f"  Drop threshold: ${detector.drop_threshold}")
    print(f"  Pump threshold: ${detector.pump_threshold}")
    print(f"  Min odds: {detector.min_odds}")
    print()

    try:
        while True:
            time.sleep(3)
            # Mock market for testing
            test_market = {
                'slug': 'btc-updown-5m-1700000000',
                'up_price': 0.50,
                'down_price': 0.50,
            }
            status = ws.get_status()
            if status['mid_price'] > 0:
                print(f"BTC: ${status['mid_price']:,.2f} | "
                      f"Window open: ${detector.window_open_price or 0:,.2f} | "
                      f"Deviation: ${status['mid_price'] - (detector.window_open_price or 0):,.2f}")

                signal = detector.check(test_market)
                if signal['action'] != 'HOLD':
                    print(f"  🔥 SIGNAL: {signal['action']} | conf={signal['confidence']:.1%}")
                    print(f"  📝 {signal.get('reason', '')}")
                    print()

    except KeyboardInterrupt:
        ws.stop()
        print("Done.")
