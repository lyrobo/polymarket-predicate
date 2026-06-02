"""Volatility Breakout Engine - Predict large moves, not direction"""

import numpy as np
from datetime import datetime, timezone
from collections import deque


class VolatilityBreakoutEngine:
    """
    Predict volatility expansion, not direction.
    
    Signals:
    - ATR compression → imminent expansion
    - Bollinger Band squeeze → breakout imminent
    - Volume anomaly → move starting
    - Keltner Channel squeeze → volatility trap
    
    Strategy: Wait for breakout, then follow direction.
    """
    
    def __init__(self, window=30):
        self.window = window
        self.history = deque(maxlen=100)
        
    def analyze(self, indicators: dict) -> dict:
        """Analyze volatility state."""
        latest = indicators.get("latest", {})
        signals = []
        score = 0.0
        
        # 1. ATR compression detection
        atr = latest.get("atr", 0)
        atr_ma = latest.get("atr_ma", 0)
        
        if atr and atr_ma and atr_ma > 0:
            atr_ratio = atr / atr_ma
            if atr_ratio < 0.7:  # ATR significantly below average
                score += 0.15
                signals.append(f"ATR compressed ({atr_ratio:.2f}x avg) → expansion imminent")
            elif atr_ratio > 1.5:  # ATR significantly above average
                signals.append(f"ATR expanded ({atr_ratio:.2f}x avg) → high volatility")
        
        # 2. Bollinger Band squeeze
        bb_width = latest.get("bb_width", 0)
        bb_width_ma = latest.get("bb_width_ma", 0)
        
        if bb_width and bb_width_ma and bb_width_ma > 0:
            bb_ratio = bb_width / bb_width_ma
            if bb_ratio < 0.6:  # BB significantly narrower than average
                score += 0.12
                signals.append(f"BB squeeze ({bb_ratio:.2f}x avg) → breakout imminent")
            elif bb_ratio > 1.5:
                signals.append(f"BB expanded ({bb_ratio:.2f}x avg) → high volatility")
        
        # 3. Volume anomaly
        volume_ratio = latest.get("volume_ratio", 1)
        if volume_ratio < 0.3:  # Very low volume = consolidation
            score += 0.08
            signals.append(f"Volume anomaly ({volume_ratio:.2f}x avg) → consolidation → breakout")
        elif volume_ratio > 2.0:  # Very high volume = move in progress
            signals.append(f"Volume spike ({volume_ratio:.2f}x avg) → move active")
        
        # 4. Price compression (low range)
        price_range = latest.get("price_range_5", 0)
        price = latest.get("close", 0)
        
        if price and price_range:
            range_pct = price_range / price * 100
            if range_pct < 0.1:  # Less than 0.1% range in 5 bars
                score += 0.10
                signals.append(f"Price compression ({range_pct:.3f}% range) → coiling")
        
        # 5. Volatility regime classification
        if atr and price:
            vol_pct = atr / price * 100
            if vol_pct < 0.05:  # Very low volatility
                regime = "extreme_compression"
                score += 0.20  # Highest edge - extreme compression
                signals.append(f"Extreme compression ({vol_pct:.3f}% vol) → major move coming")
            elif vol_pct < 0.1:
                regime = "low_volatility"
                score += 0.10
                signals.append(f"Low volatility ({vol_pct:.3f}% vol) → coiling")
            elif vol_pct > 0.3:
                regime = "high_volatility"
                signals.append(f"High volatility ({vol_pct:.3f}% vol) → trending")
            else:
                regime = "normal"
        
        confidence = min(1.0, 0.5 + score)
        
        return {
            "type": "volatility_breakout",
            "confidence": float(confidence),
            "score": float(score),
            "regime": regime if 'regime' in dir() else "unknown",
            "signals": signals,
            "action": "WATCH" if confidence > 0.65 else "WAIT",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


class BreakoutDetector:
    """Detect actual breakout events."""
    
    def __init__(self):
        self.last_breakout = None
        
    def detect(self, indicators: dict, current_price: float) -> dict:
        """Detect if price is breaking out."""
        latest = indicators.get("latest", {})
        signals = []
        
        # 1. Price breaking BB bands
        bb_upper = latest.get("bb_upper", 0)
        bb_lower = latest.get("bb_lower", 0)
        
        if current_price > bb_upper:
            signals.append(f"Price breaking upper BB (${current_price:.0f} > ${bb_upper:.0f})")
            direction = "UP"
        elif current_price < bb_lower:
            signals.append(f"Price breaking lower BB (${current_price:.0f} < ${bb_lower:.0f})")
            direction = "DOWN"
        else:
            direction = "NONE"
        
        # 2. Volume confirmation
        volume_ratio = latest.get("volume_ratio", 1)
        if volume_ratio > 1.5 and direction != "NONE":
            signals.append(f"Volume confirms breakout ({volume_ratio:.2f}x)")
            confidence = 0.75
        elif direction != "NONE":
            confidence = 0.55
        else:
            confidence = 0.5
        
        return {
            "type": "breakout_detected",
            "direction": direction,
            "confidence": float(confidence),
            "signals": signals,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
