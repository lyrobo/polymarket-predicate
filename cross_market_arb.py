"""
Cross-Market Statistical Arbitrage Strategy
=============================================
Based on the successful Polymarket bot (0xce25... ~75% WR, $141K/mo).

Core formulas:
  S_t = P_{P,t} - β * P_{K,t} - μ          (cross-market spread)
  dS_t = θ * (μ - S_t) * dt + σ * dW_t     (OU mean reversion)
  I_t = (V_b - V_a) / (V_b + V_a)          (L2 order book imbalance)
  P_micro = (V_b*P_a + V_a*P_b) / (V_b+V_a) (micro price)

When Kalshi data is unavailable, we approximate P_K with spot BTC
implied probability: P_K ≈ 0.5 + α * (spot_return / σ_5min)
"""

import time
import logging
import numpy as np
from datetime import datetime, timezone
from collections import deque
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class CrossMarketArbEngine:
    """Cross-market statistical arbitrage between Polymarket and
    implied fair value (Kalshi or spot-derived).
    """

    def __init__(
        self,
        ou_theta: float = 0.15,       # mean reversion speed
        ou_mu: float = 0.0,           # long-run spread mean
        ou_sigma: float = 0.02,       # spread volatility
        entry_threshold: float = 2.0,  # sigma threshold to enter
        exit_threshold: float = 0.3,   # sigma threshold to exit
        min_orderbook_imbalance: float = 0.15,  # |I_t| must exceed this
        lookback_seconds: int = 300,   # spread history window
        min_confidence: float = 0.70,
    ):
        self.theta = ou_theta
        self.mu = ou_mu
        self.sigma = ou_sigma
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.min_imbalance = min_orderbook_imbalance
        self.lookback = lookback_seconds
        self.min_confidence = min_confidence

        self.ws = None
        self.spread_history = deque(maxlen=300)  # (timestamp, S_t)
        self.last_position: Optional[Dict] = None

    def attach_websocket(self, ws_client):
        self.ws = ws_client
        logger.info("CrossMarketArbEngine: WebSocket attached")

    # ── L2 Order Book Imbalance ──────────────────────────────

    def compute_imbalance(self) -> float:
        """I_t = (V_bid - V_ask) / (V_bid + V_ask)

        Positive = buying pressure → fair price above mid
        Negative = selling pressure → fair price below mid
        """
        if self.ws is None:
            return 0.0
        ob = self.ws.order_book
        snapshot = ob.get_snapshot()
        bids = snapshot.get('bids', [])
        asks = snapshot.get('asks', [])

        V_b = sum(q for _, q in bids[:10])
        V_a = sum(q for _, q in asks[:10])
        total = V_b + V_a
        return (V_b - V_a) / total if total > 0 else 0.0

    # ── Micro Price ──────────────────────────────────────────

    def compute_micro_price(self) -> Optional[float]:
        """P_micro = (V_bid * P_ask + V_ask * P_bid) / (V_bid + V_ask)

        Weighted price reflecting order book pressure.
        When P_micro > mid_price → buying pressure lifting fair value.
        """
        if self.ws is None:
            return None
        ob = self.ws.order_book
        snapshot = ob.get_snapshot()
        bids = snapshot.get('bids', [])
        asks = snapshot.get('asks', [])

        if not bids or not asks:
            return None

        V_b = sum(q for _, q in bids[:5])
        V_a = sum(q for _, q in asks[:5])
        P_b = bids[0][0]  # best bid
        P_a = asks[0][0]  # best ask

        total = V_b + V_a
        if total == 0:
            return None

        return (V_b * P_a + V_a * P_b) / total

    # ── Cross-Market Spread (S_t) ────────────────────────────

    def compute_spread(self, polymarket_up: float, polymarket_down: float) -> float:
        """S_t = P_{P} - P_{fair}

        When S_t > 0: Polymarket UP overpriced vs fair → sell UP / buy DOWN
        When S_t < 0: Polymarket UP underpriced vs fair → buy UP / sell DOWN

        Fair price derived from:
          (a) Kalshi market (if available) — P_K
          (b) Otherwise: spot-derived implied probability

        For now: P_fair ≈ 0.5 + I_t * 0.05 + micro_price_adjustment
        where I_t is the order book imbalance signal.
        """
        I_t = self.compute_imbalance()
        micro = self.compute_micro_price()
        mid = self.ws.order_book.get_mid_price() if self.ws else 0

        # Implied fair probability from order book
        # Positive imbalance → more buying pressure → UP more likely
        fair_prob = 0.5 + I_t * 0.10

        # Adjust for micro price deviation
        if micro and mid > 0:
            micro_dev = (micro - mid) / mid
            fair_prob += micro_dev * 0.05

        fair_prob = max(0.01, min(0.99, fair_prob))

        # Polymarket mid price
        P_p = polymarket_up

        # Spread: positive = Polymarket UP overpriced
        S_t = P_p - fair_prob

        return S_t

    # ── OU Process Z-Score ───────────────────────────────────

    def compute_zscore(self, S_t: float) -> float:
        """Compute z-score of current spread vs historical distribution.

        z = (S_t - mean(S_history)) / std(S_history)
        |z| > entry_threshold → trade signal
        |z| < exit_threshold → exit signal
        """
        now = time.time()
        self.spread_history.append((now, S_t))

        # Clean old entries
        cutoff = now - self.lookback
        while self.spread_history and self.spread_history[0][0] < cutoff:
            self.spread_history.popleft()

        if len(self.spread_history) < 10:
            return 0.0

        spreads = [s for _, s in self.spread_history]
        mean_s = np.mean(spreads)
        std_s = np.std(spreads) + 1e-10

        return (S_t - mean_s) / std_s

    # ── Main Signal ──────────────────────────────────────────

    def analyze(
        self,
        polymarket_up: float,
        polymarket_down: float,
        btc_price: float,
    ) -> Dict:
        """Run full analysis. Returns trading signal."""

        # Compute spread
        S_t = self.compute_spread(polymarket_up, polymarket_down)
        z = self.compute_zscore(S_t)
        I_t = self.compute_imbalance()
        micro = self.compute_micro_price()
        mid = self.ws.order_book.get_mid_price() if self.ws else btc_price

        # Signal logic
        action = 'HOLD'
        confidence = 0.0
        reason = ''

        if abs(z) > self.entry_threshold and abs(I_t) > self.min_imbalance:
            # Confirm with both spread z-score AND order book imbalance
            if z < 0 and I_t > 0:
                # z<0: Polymarket UP underpriced + I>0: buying pressure
                # → buy UP (Polymarket will correct upward)
                action = 'BUY_UP'
                confidence = min(0.95, 0.65 + abs(z) * 0.05 + abs(I_t) * 0.3)
                reason = (
                    f'Spread z={z:.1f} (UP underpriced), '
                    f'I_t={I_t:.2%} (buy pressure) → mean reversion UP'
                )
            elif z > 0 and I_t < 0:
                # z>0: Polymarket UP overpriced + I<0: selling pressure
                # → buy DOWN
                action = 'BUY_DN'
                confidence = min(0.95, 0.65 + abs(z) * 0.05 + abs(I_t) * 0.3)
                reason = (
                    f'Spread z={z:.1f} (UP overpriced), '
                    f'I_t={I_t:.2%} (sell pressure) → mean reversion DOWN'
                )

        # Exit logic
        if self.last_position and abs(z) < self.exit_threshold:
            action = 'EXIT'
            reason = f'Spread converged (z={z:.1f})'

        return {
            'action': action,
            'confidence': round(confidence, 4),
            'reason': reason,
            'spread': round(S_t, 6),
            'zscore': round(z, 3),
            'imbalance': round(I_t, 4),
            'micro_price': round(micro, 2) if micro else None,
            'mid_price': round(mid, 2),
            'fair_prob': round(0.5 + S_t - polymarket_up + 0.5, 4) if polymarket_up > 0 else 0.5,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }


# Quick test
if __name__ == '__main__':
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    from websocket_collector import WebSocketClient

    engine = CrossMarketArbEngine()
    ws = WebSocketClient(symbol='BTC-USDT')
    engine.attach_websocket(ws)
    ws.start()

    print('CrossMarketArb engine — Ctrl+C to stop')
    time.sleep(10)

    try:
        while True:
            time.sleep(5)
            signal = engine.analyze(
                polymarket_up=0.505,
                polymarket_down=0.495,
                btc_price=ws.order_book.get_mid_price(),
            )
            if signal['action'] != 'HOLD':
                print(f'\n🔥 {signal["action"]} | conf={signal["confidence"]:.1%} | '
                      f'z={signal["zscore"]:.2f} I={signal["imbalance"]:.2%}')
                print(f'   {signal["reason"]}')
            else:
                print(f'   HOLD | z={signal["zscore"]:.2f} I={signal["imbalance"]:.2%} '
                      f'S={signal["spread"]:.4f}', end='\r')
    except KeyboardInterrupt:
        ws.stop()
        print('\nDone.')
