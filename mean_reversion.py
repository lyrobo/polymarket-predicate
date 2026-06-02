"""Mean Reversion Engine - Micro mean reversion for 5-minute trades"""

import numpy as np
from datetime import datetime, timezone
from collections import deque


class MeanReversionEngine:
    """
    Detect short-term mean reversion opportunities.
    
    Signals:
    - Price deviation from VWAP
    - Order book imbalance reversal
    - Short-term overextension
    - Liquidity void detection
    
    Strategy: Fade extreme moves, expect reversion.
    """
    
    def __init__(self, window=20):
        self.window = window
        self.price_history = deque(maxlen=100)
        self.vwap_history = deque(maxlen=100)
        
    def compute_vwap(self, klines: list) -> float:
        """Compute Volume Weighted Average Price."""
        if not klines:
            return 0
        
        total_volume = 0
        total_vwap = 0
        
        for k in klines:
            typical_price = (k.get('high', 0) + k.get('low', 0) + k.get('close', 0)) / 3
            volume = k.get('volume', 0)
            total_vwap += typical_price * volume
            total_volume += volume
        
        return total_vwap / total_volume if total_volume > 0 else 0
    
    def analyze(self, indicators: dict, klines: list = None) -> dict:
        """Analyze mean reversion signals."""
        latest = indicators.get("latest", {})
        signals = []
        score = 0.0
        
        # 1. VWAP deviation
        current_price = latest.get("close", 0)
        
        vwap = 0  # Default, set below if klines available
        
        if klines and len(klines) >= 5:
            vwap = self.compute_vwap(klines[-20:])  # Last 20 bars
            self.vwap_history.append({"vwap": vwap, "price": current_price, "timestamp": datetime.now(timezone.utc).isoformat()})
            
            if vwap > 0:
                deviation = (current_price - vwap) / vwap * 100
                
                if abs(deviation) > 0.15:  # More than 0.15% deviation
                    if deviation > 0:
                        score -= 0.12  # Price above VWAP → expect reversion down
                        signals.append(f"Price above VWAP by {deviation:.2f}% → mean reversion DOWN")
                    else:
                        score += 0.12  # Price below VWAP → expect reversion up
                        signals.append(f"Price below VWAP by {abs(deviation):.2f}% → mean reversion UP")
        
        # 2. Short-term overextension (z-score)
        if klines and len(klines) >= 10:
            returns = []
            for i in range(1, min(10, len(klines))):
                if klines[i-1].get('close', 0) > 0:
                    ret = (klines[i].get('close', 0) - klines[i-1].get('close', 0)) / klines[i-1].get('close', 0)
                    returns.append(ret)
            
            if returns:
                ret_array = np.array(returns)
                z_score = (ret_array[-1] - np.mean(ret_array)) / (np.std(ret_array) + 1e-10)
                
                if abs(z_score) > 2.0:  # Extreme move
                    if z_score > 0:
                        score -= 0.10
                        signals.append(f"Extreme upward move (z={z_score:.1f}) → reversion likely")
                    else:
                        score += 0.10
                        signals.append(f"Extreme downward move (z={z_score:.1f}) → reversion likely")
        
        # 3. RSI divergence from price
        rsi = latest.get("rsi", 50)
        if rsi is not None and not np.isnan(rsi):
            if rsi > 80:  # Extremely overbought
                score -= 0.15
                signals.append(f"RSI extremely overbought ({rsi:.1f}) → mean reversion")
            elif rsi < 20:  # Extremely oversold
                score += 0.15
                signals.append(f"RSI extremely oversold ({rsi:.1f}) → mean reversion")
        
        # 4. Bollinger Band touch
        bb_upper = latest.get("bb_upper", 0)
        bb_lower = latest.get("bb_lower", 0)
        price_position = latest.get("price_position", 0.5)
        
        if price_position > 0.95:  # Near or above upper band
            score -= 0.08
            signals.append("Price at upper BB → bounce back expected")
        elif price_position < 0.05:  # Near or below lower band
            score += 0.08
            signals.append("Price at lower BB → bounce up expected")
        
        # 5. Volume-price divergence
        volume_ratio = latest.get("volume_ratio", 1)
        ret_1 = latest.get("returns_1", 0)
        
        if volume_ratio < 0.5 and abs(ret_1) > 0.001:
            # Low volume move → likely false move
            if ret_1 > 0:
                score -= 0.06
                signals.append("Low volume upward move → likely false breakout")
            else:
                score += 0.06
                signals.append("Low volume downward move → likely false breakdown")
        
        confidence = max(0.0, min(1.0, 0.5 + score))
        # Neutral zone: return NEUTRAL instead of random direction
        if abs(score) < 0.05:
            direction = 0
        else:
            direction = 1 if confidence > 0.5 else -1
        
        return {
            "type": "mean_reversion",
            "direction": direction,
            "confidence": float(confidence),
            "score": float(score),
            "signals": signals,
            "vwap_deviation": float((current_price - vwap) / vwap * 100) if vwap > 0 else 0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


class LiquidityAnalyzer:
    """Analyze liquidity conditions for mean reversion."""
    
    def __init__(self):
        self.spread_history = deque(maxlen=50)
        
    def analyze(self, order_book: dict) -> dict:
        """Analyze liquidity conditions."""
        if not order_book:
            return {"type": "liquidity", "signals": ["No order book data"]}
        
        signals = []
        
        # 1. Spread analysis
        spread_bps = order_book.get("spread_bps", 0)
        self.spread_history.append(spread_bps)
        
        if len(self.spread_history) >= 10:
            avg_spread = np.mean(list(self.spread_history)[-10:])
            
            if spread_bps < avg_spread * 0.5:  # Tight spread
                signals.append(f"Tight spread ({spread_bps:.1f} bps vs avg {avg_spread:.1f}) → good liquidity")
            elif spread_bps > avg_spread * 2.0:  # Wide spread
                signals.append(f"Wide spread ({spread_bps:.1f} bps vs avg {avg_spread:.1f}) → poor liquidity")
        
        # 2. Depth imbalance
        bid_depth = order_book.get("bid_depth", 0)
        ask_depth = order_book.get("ask_depth", 0)
        
        if bid_depth > 0 and ask_depth > 0:
            depth_ratio = bid_depth / ask_depth
            
            if depth_ratio > 1.5:  # More bid depth
                signals.append(f"Bid-heavy depth ({depth_ratio:.2f}x) → support below")
            elif depth_ratio < 0.67:  # More ask depth
                signals.append(f"Ask-heavy depth ({depth_ratio:.2f}x) → resistance above")
        
        return {
            "type": "liquidity",
            "signals": signals,
            "spread_bps": spread_bps,
            "depth_ratio": float(depth_ratio) if (bid_depth > 0 and ask_depth > 0) else 1.0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
