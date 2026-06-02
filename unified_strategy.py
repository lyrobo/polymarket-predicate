"""Unified Strategy Engine - Integrates all four prediction directions"""

import json
import time
import logging
import numpy as np
from datetime import datetime, timezone
from config import *
from technical_analysis import TechnicalAnalyzer
from order_flow import OrderFlowEngine
from volatility_breakout import VolatilityBreakoutEngine, BreakoutDetector
from mean_reversion import MeanReversionEngine, LiquidityAnalyzer
from event_driven import EventDrivenEngine, NewsAnalyzer

logger = logging.getLogger(__name__)


def score_to_confidence(score: float, scale: float = 2.5) -> float:
    """Calibrated sigmoid mapping: raw score → probability [0, 1].

    score=0 → 0.50 (neutral)
    score=0.2 → 0.62
    score=0.5 → 0.78
    score=1.0 → 0.92
    """
    return 1.0 / (1.0 + np.exp(-score * scale))


class UnifiedStrategyEngine:
    """
    Unified prediction engine integrating four directions:
    1. Order Flow (most important)
    2. Volatility Breakout
    3. Mean Reversion
    4. Event-Driven (modulates regime, not direction)
    """

    def __init__(self):
        self.order_flow = OrderFlowEngine()
        self.vol_breakout = VolatilityBreakoutEngine()
        self.breakout_detector = BreakoutDetector()
        self.mean_reversion = MeanReversionEngine()
        self.liquidity = LiquidityAnalyzer()
        self.event_driven = EventDrivenEngine()
        self.news = NewsAnalyzer()
        self.analyzer = TechnicalAnalyzer()

        # WebSocket client (optional, for real-time data)
        self.ws_client = None

        # Weights for each module (can be adapted over time)
        self.weights = {
            "order_flow": 0.35,
            "volatility": 0.25,
            "mean_reversion": 0.25,
            "event_driven": 0.15,
        }

        # Performance tracking for adaptive weights
        self._module_correct = {k: 0 for k in self.weights}
        self._module_total = {k: 0 for k in self.weights}
        self._last_direction = None
        self._last_entry_price = None
        self._adapt_interval = 100
        self._predictions_since_adapt = 0

        self.prediction_history = []

    def attach_websocket(self, ws_client):
        """Attach WebSocket client for real-time order flow data."""
        self.ws_client = ws_client
        self.order_flow.attach_websocket(ws_client)
        logger.info("WebSocket attached to UnifiedStrategyEngine")

    def predict(self, klines: list = None, order_book: dict = None) -> dict:
        """Run unified prediction."""
        # Compute indicators from klines
        indicators = {}
        if klines and len(klines) >= 30:
            indicators = self.analyzer.compute_all(klines)

        # 1. Order Flow Analysis
        of_result = self.order_flow.analyze()

        # 2. Volatility Breakout Analysis
        vol_result = self.vol_breakout.analyze(indicators)
        breakout_result = self.breakout_detector.detect(
            indicators, indicators.get("latest", {}).get("close", 0)
        ) if indicators else None

        # 3. Mean Reversion Analysis
        mr_result = self.mean_reversion.analyze(indicators, klines)
        liq_result = self.liquidity.analyze(order_book) if order_book else None

        # 4. Event-Driven Analysis (regime context, not directional)
        event_result = self.event_driven.analyze()
        news_result = self.news.check_news()

        # Combine predictions
        combined = self._combine_predictions(
            of_result, vol_result, breakout_result, mr_result, liq_result,
            event_result, news_result, indicators
        )

        # Store prediction
        self.prediction_history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "combined": combined,
            "order_flow": of_result,
            "volatility": vol_result,
            "mean_reversion": mr_result,
            "event_driven": event_result,
        })

        # Keep only last 200 predictions
        if len(self.prediction_history) > 200:
            self.prediction_history = self.prediction_history[-200:]

        self._predictions_since_adapt += 1
        self._last_direction = combined["direction"]
        if indicators:
            self._last_entry_price = indicators.get("latest", {}).get("close")

        return combined

    def record_outcome(self, actual_direction: int):
        """Record actual outcome to adapt weights."""
        if self._last_direction is None:
            return

        for module_name in self.weights:
            mod_conf = self.prediction_history[-1]["combined"]["modules"].get(
                module_name, 0.5
            ) if self.prediction_history else 0.5

            # Check if this module's direction was correct
            module_dir = 1 if mod_conf > 0.5 else -1
            self._module_total[module_name] = (self._module_total.get(module_name, 0) + 1)
            if module_dir == actual_direction:
                self._module_correct[module_name] = (self._module_correct.get(module_name, 0) + 1)

        if self._predictions_since_adapt >= self._adapt_interval:
            self._adapt_weights()
            self._predictions_since_adapt = 0

    def _adapt_weights(self):
        """Adjust module weights based on recent accuracy."""
        accuracies = {}
        for name in self.weights:
            total = self._module_total.get(name, 0)
            correct = self._module_correct.get(name, 0)
            accuracies[name] = correct / max(total, 1)

        # Only adapt if we have meaningful data
        min_samples = sum(self._module_total.values())
        if min_samples < 50:
            return

        # Keep base weights but shift toward better performers
        base_weights = {
            "order_flow": 0.35,
            "volatility": 0.25,
            "mean_reversion": 0.25,
            "event_driven": 0.15,
        }

        # Blend: 70% base + 30% performance
        perf_weights = {}
        total_acc = sum(max(a, 0.45) for a in accuracies.values())
        if total_acc > 0:
            for name in self.weights:
                perf_weights[name] = max(accuracies[name], 0.45) / total_acc

        for name in self.weights:
            self.weights[name] = base_weights[name] * 0.7 + perf_weights.get(name, base_weights[name]) * 0.3

        # Normalize
        total = sum(self.weights.values())
        if total > 0:
            for name in self.weights:
                self.weights[name] /= total

        logger.info(
            f"Weights adapted: "
            + " ".join(f"{k}={v:.2f}" for k, v in self.weights.items())
        )

    def _combine_predictions(self, of_result, vol_result, breakout_result, mr_result,
                             liq_result, event_result, news_result, indicators) -> dict:
        """Combine all predictions into unified signal."""

        signals = []
        weighted_score = 0.0
        total_weight = 0.0

        # Get regime from event-driven (used to modulate thresholds)
        regime = event_result.get("regime", "normal")
        regime_vol = event_result.get("volatility", "medium")

        # --- 1. Order Flow (35%) ---
        of_direction = of_result.get("direction", 0)
        of_raw_score = of_result.get("score", 0)
        if of_raw_score != 0:
            of_signals = of_result.get("signals", [])
            signals.extend(of_signals)
            weighted_score += self.weights["order_flow"] * of_raw_score
            total_weight += self.weights["order_flow"]

        # --- 2. Volatility Breakout (25%) ---
        vol_raw_score = vol_result.get("score", 0)
        vol_confidence_raw = vol_result.get("confidence", 0.5)

        # If breakout detected, follow direction with higher weight
        if breakout_result and breakout_result.get("direction") != "NONE":
            bv_direction = 1 if breakout_result["direction"] == "UP" else -1
            bv_confidence = breakout_result.get("confidence", 0.5)
            # Convert breakout confidence to score
            bv_score = (bv_confidence - 0.5) * 2 * bv_direction
            weighted_score += self.weights["volatility"] * bv_score
            if breakout_result.get("signals"):
                signals.extend(breakout_result["signals"])
        elif vol_raw_score > 0:
            # Volatility building — increase regime sensitivity
            if vol_confidence_raw > 0.65:
                signals.append("⏳ Volatility building — awaiting breakout direction")
            # Don't add directional score from volatility module alone
        else:
            # Low volatility — signals are weak, note it
            pass
        total_weight += self.weights["volatility"]

        # --- 3. Mean Reversion (25%) ---
        mr_direction = mr_result.get("direction", 0)
        mr_raw_score = mr_result.get("score", 0)
        mr_signals = mr_result.get("signals", [])
        if mr_signals:
            signals.extend(mr_signals)
            # Mean reversion: score already encodes direction
            weighted_score += self.weights["mean_reversion"] * mr_raw_score
            total_weight += self.weights["mean_reversion"]
        else:
            total_weight += self.weights["mean_reversion"]

        # --- 4. Event-Driven (15%) — regime modulation ---
        event_raw_score = event_result.get("score", 0)
        event_signals = event_result.get("signals", [])
        if event_signals:
            signals.extend(event_signals)

        # Event-driven provides regime context, not direction.
        # It modulates the overall confidence based on volatility regime:
        regime_multiplier = {
            "very_high": 1.3,   # More confidence in high vol
            "high": 1.15,
            "medium": 1.0,
            "low": 0.85,        # Less confidence in low vol
        }.get(regime_vol, 1.0)

        # Upcoming high-impact events increase expected move size
        upcoming = event_result.get("upcoming_events", [])
        high_impact_soon = any(
            e.get("impact") == "high" and e.get("minutes_until", 999) < 30
            for e in upcoming
        )
        if high_impact_soon:
            regime_multiplier = max(regime_multiplier, 1.25)

        # Add a small regime score contribution
        weighted_score += self.weights["event_driven"] * event_raw_score * 0.3
        total_weight += self.weights["event_driven"]

        # --- Compute final signal ---
        if total_weight > 0:
            normalized_score = weighted_score / total_weight
        else:
            normalized_score = 0

        # Apply regime multiplier
        normalized_score *= regime_multiplier

        # Direction: preserved from sign of normalized score
        if abs(normalized_score) < 0.02:
            direction = 0  # NEUTRAL — no coin flip!
        else:
            direction = 1 if normalized_score > 0 else -1

        # Convert to calibrated confidence
        if direction == 0:
            confidence = 0.5
        else:
            confidence = score_to_confidence(abs(normalized_score), scale=2.5)

        # Determine action
        if confidence > 0.65:
            action = "STRONG_SIGNAL"
        elif confidence > 0.58:
            action = "WEAK_SIGNAL"
        elif confidence > 0.53:
            action = "WATCH"
        else:
            action = "WAIT"

        # Check for conflicting signals
        conflicts = self._detect_conflicts(of_result, mr_result, breakout_result)
        if conflicts:
            signals.extend(conflicts)
            confidence *= 0.85  # Reduce confidence on conflicts

        return {
            "direction": direction,
            "confidence": float(confidence),
            "score": float(normalized_score),
            "action": action,
            "signals": signals,
            "regime": regime,
            "regime_multiplier": float(regime_multiplier),
            "weights_used": {k: round(v, 2) for k, v in self.weights.items() if v > 0},
            "modules": {
                "order_flow": of_result.get("confidence", 0.5),
                "volatility": vol_result.get("confidence", 0.5),
                "mean_reversion": mr_result.get("confidence", 0.5),
                "event_driven": event_result.get("confidence", 0.5),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _detect_conflicts(self, of_result, mr_result, breakout_result) -> list:
        """Detect conflicting signals between modules."""
        conflicts = []

        of_dir = of_result.get("direction", 0)
        mr_dir = mr_result.get("direction", 0)
        bv_dir = (
            1 if breakout_result and breakout_result.get("direction") == "UP"
            else -1 if breakout_result and breakout_result.get("direction") == "DOWN"
            else 0
        )

        # Order Flow vs Mean Reversion conflict
        if of_dir != 0 and mr_dir != 0 and of_dir != mr_dir:
            conflicts.append("⚠️ Order Flow and Mean Reversion disagree")

        # Breakout vs Mean Reversion conflict
        if bv_dir != 0 and mr_dir != 0 and bv_dir != mr_dir:
            conflicts.append("⚠️ Breakout and Mean Reversion disagree")

        return conflicts

    def get_stats(self) -> dict:
        """Get prediction statistics."""
        if not self.prediction_history:
            return {"total_predictions": 0}

        recent = self.prediction_history[-50:]
        directions = [p["combined"]["direction"] for p in recent]
        confidences = [p["combined"]["confidence"] for p in recent]
        actions = [p["combined"]["action"] for p in recent]

        return {
            "total_predictions": len(self.prediction_history),
            "recent_predictions": len(recent),
            "avg_confidence": float(np.mean(confidences)),
            "neutral_count": directions.count(0),
            "direction_distribution": {
                "UP": directions.count(1),
                "DOWN": directions.count(-1),
                "NEUTRAL": directions.count(0),
            },
            "action_distribution": {
                "STRONG_SIGNAL": actions.count("STRONG_SIGNAL"),
                "WEAK_SIGNAL": actions.count("WEAK_SIGNAL"),
                "WATCH": actions.count("WATCH"),
                "WAIT": actions.count("WAIT"),
            },
            "module_weights": self.weights.copy(),
            "latest": self.prediction_history[-1]["combined"] if self.prediction_history else None,
        }


if __name__ == "__main__":
    engine = UnifiedStrategyEngine()

    print("Unified Strategy Engine (Optimized)")
    print("=" * 60)

    for i in range(3):
        result = engine.predict()
        print(f"\n[{result['timestamp']}]")
        print(f"  Direction: {'UP' if result['direction']==1 else 'DN' if result['direction']==-1 else 'NEUTRAL'} "
              f"({result['confidence']:.1%})")
        print(f"  Action: {result['action']}")
        print(f"  Regime: {result.get('regime', 'N/A')} (x{result.get('regime_multiplier', 1.0)})")
        print(f"  Module Confidences:")
        for mod, conf in result['modules'].items():
            print(f"    - {mod}: {conf:.1%}")
        print(f"  Signals:")
        for s in result['signals']:
            print(f"    - {s}")

        time.sleep(2)
