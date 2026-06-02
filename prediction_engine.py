"""Prediction Engine - Gradient Boosting ML + Rule-based ensemble for BTC 5-min"""

import json
import pickle
import logging
import numpy as np
from pathlib import Path
from datetime import datetime, timezone
from config import *

logger = logging.getLogger(__name__)

# Try importing sklearn
try:
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.model_selection import train_test_split
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    logger.warning("scikit-learn not available — using fallback Random Forest")


# ============================================================================
# Rule-Based Predictor
# ============================================================================

class RuleBasedPredictor:
    """Rule-based prediction using enhanced technical indicators."""

    def predict(self, indicators: dict) -> dict:
        latest = indicators.get("latest", {})
        signals = []
        score = 0.0

        # --- RSI ---
        rsi = latest.get("rsi", 50)
        if rsi is not None and not np.isnan(rsi):
            if rsi < 30:
                score += 0.12
                signals.append(f"RSI oversold ({rsi:.1f}) → BULLISH")
            elif rsi > 70:
                score -= 0.12
                signals.append(f"RSI overbought ({rsi:.1f}) → BEARISH")
            elif rsi < 40:
                score += 0.04
            elif rsi > 60:
                score -= 0.04

        # --- MACD ---
        macd_hist = latest.get("macd_hist", 0)
        if macd_hist is not None and not np.isnan(macd_hist):
            macd_line = latest.get("macd", 0)
            macd_signal = latest.get("macd_signal", 0)

            # Histogram direction
            if macd_hist > 0:
                score += 0.08
                signals.append(f"MACD positive ({macd_hist:.2f}) → BULLISH")
            else:
                score -= 0.08
                signals.append(f"MACD negative ({macd_hist:.2f}) → BEARISH")

            # MACD crossover bias
            if macd_line > macd_signal:
                score += 0.04
            else:
                score -= 0.04

        # --- EMA Crossover ---
        ema_5 = latest.get("ema_5", 0)
        ema_20 = latest.get("ema_20", 0)
        if ema_5 and ema_20 and not (np.isnan(ema_5) or np.isnan(ema_20)):
            if ema_5 > ema_20:
                score += 0.06
            else:
                score -= 0.06

        # --- Bollinger Band Position ---
        bb_pos = latest.get("price_position", 0.5)
        if bb_pos is not None and not np.isnan(bb_pos):
            if bb_pos < 0.15:
                score += 0.10
                signals.append("Price near lower BB → BOUNCE")
            elif bb_pos > 0.85:
                score -= 0.10
                signals.append("Price near upper BB → PULLBACK")

        # --- BB Squeeze ---
        bb_width = latest.get("bb_width", 0)
        bb_width_ma = latest.get("bb_width_ma", 0)
        if bb_width and bb_width_ma and bb_width_ma > 0:
            bb_ratio = bb_width / bb_width_ma
            if bb_ratio < 0.6:
                signals.append(f"BB squeeze ({bb_ratio:.2f}) → breakout imminent")

        # --- Volume ---
        vol_ratio = latest.get("volume_ratio", 1)
        if vol_ratio is not None and not np.isnan(vol_ratio):
            if vol_ratio > 1.8:
                signals.append(f"Volume spike ({vol_ratio:.1f}x) → move active")
                # Amplify existing trend
                if score > 0:
                    score += 0.04
                elif score < 0:
                    score -= 0.04
            elif vol_ratio < 0.4:
                signals.append("Low volume → weak conviction")

        # --- Momentum ---
        ret_1 = latest.get("returns_1", 0)
        ret_5 = latest.get("returns_5", 0)
        if ret_1 is not None and not np.isnan(ret_1):
            if ret_1 > 0.001:
                score += 0.04
            elif ret_1 < -0.001:
                score -= 0.04

        if ret_5 is not None and not np.isnan(ret_5):
            if ret_5 > 0.003:
                score += 0.06
                signals.append("5-period uptrend")
            elif ret_5 < -0.003:
                score -= 0.06
                signals.append("5-period downtrend")

        # --- ATR Compression ---
        atr_pct = latest.get("atr_pct", 0)
        atr_pct_ma = latest.get("atr_pct_ma", 0)
        if atr_pct and atr_pct_ma and atr_pct_ma > 0 and not np.isnan(atr_pct):
            atr_ratio = atr_pct / atr_pct_ma
            if atr_ratio < 0.5:
                signals.append(f"ATR compressed ({atr_ratio:.2f}) → expansion coming")

        confidence = max(0.0, min(1.0, 0.5 + score / 1.5))
        direction = 1 if confidence > 0.5 else (-1 if confidence < 0.5 else 0)

        return {
            "method": "rule_based",
            "direction": direction,
            "confidence": float(confidence),
            "score": float(score),
            "signals": signals,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ============================================================================
# ML Predictor (Gradient Boosting)
# ============================================================================

class MLPredictor:
    """Gradient Boosting predictor with probability calibration."""

    def __init__(self, n_estimators: int = 100):
        self.n_estimators = n_estimators
        self.model = None
        self.is_trained = False
        self.model_path = MODEL_DIR / "ml_model.pkl"
        self._load_model()

    def _load_model(self):
        if self.model_path.exists() and SKLEARN_AVAILABLE:
            try:
                with open(self.model_path, "rb") as f:
                    self.model = pickle.load(f)
                self.is_trained = True
                logger.info(f"Loaded ML model from {self.model_path}")
            except Exception as e:
                logger.warning(f"Load ML model failed: {e}")

    def _save_model(self):
        if self.model and SKLEARN_AVAILABLE:
            try:
                with open(self.model_path, "wb") as f:
                    pickle.dump(self.model, f)
                logger.info(f"Saved ML model to {self.model_path}")
            except Exception as e:
                logger.warning(f"Save ML model failed: {e}")

    def train(self, features: np.ndarray, labels: np.ndarray):
        """Train Gradient Boosting model with calibration."""
        if not SKLEARN_AVAILABLE:
            logger.warning("sklearn not available — skipping ML training")
            return

        if len(features) < 50:
            logger.warning(f"Insufficient training data: {len(features)}")
            return

        # Remove NaN rows
        valid = ~np.isnan(features).any(axis=1)
        features = features[valid]
        labels = labels[valid]

        if len(features) < 50:
            return

        # Balance classes
        up_count = np.sum(labels == 1)
        down_count = np.sum(labels == -1)
        min_class = min(up_count, down_count)

        if min_class < 10:
            logger.warning(f"Too few samples in minority class: {min_class}")
            return

        logger.info(f"Training ML: {len(features)} samples, UP={up_count}, DOWN={down_count}")

        # Gradient Boosting with conservative params to avoid overfitting
        gb = GradientBoostingClassifier(
            n_estimators=min(self.n_estimators, len(features) // 10),
            max_depth=3,
            learning_rate=0.05,
            subsample=0.8,
            min_samples_leaf=20,
            max_features='sqrt',
            random_state=42,
        )

        # Fit
        gb.fit(features, labels)

        # Calibrate probabilities
        try:
            self.model = CalibratedClassifierCV(gb, method='isotonic', cv=3)
            self.model.fit(features, labels)
            logger.info(f"Calibrated GB model: {gb.n_estimators} trees, depth={gb.max_depth}")
        except Exception as e:
            logger.warning(f"Calibration failed: {e}, using raw model")
            self.model = gb

        self.is_trained = True
        self._save_model()

    def predict(self, feature_vector: np.ndarray) -> dict:
        """Predict with probability output."""
        if not self.is_trained or self.model is None:
            return {
                "method": "ml",
                "direction": 0,
                "confidence": 0.5,
                "signals": ["ML not trained yet"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        # Handle NaN
        if np.isnan(feature_vector).any():
            return {
                "method": "ml",
                "direction": 0,
                "confidence": 0.5,
                "signals": ["ML: NaN in features"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        try:
            # predict_proba returns [prob_down, prob_up]
            probs = self.model.predict_proba([feature_vector])[0]
            # Classes are [-1, 1]
            classes = self.model.classes_

            prob_down = probs[list(classes).index(-1)] if -1 in classes else 0.5
            prob_up = probs[list(classes).index(1)] if 1 in classes else 0.5

            confidence = max(prob_up, prob_down)
            direction = 1 if prob_up > prob_down else -1

            return {
                "method": "ml",
                "direction": direction,
                "confidence": float(confidence),
                "prob_up": float(prob_up),
                "prob_down": float(prob_down),
                "signals": [f"ML: {direction} @ {confidence:.1%}"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            logger.warning(f"ML predict error: {e}")
            return {
                "method": "ml",
                "direction": 0,
                "confidence": 0.5,
                "signals": [f"ML error: {e}"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }


# ============================================================================
# Prediction Engine (Ensemble)
# ============================================================================

class PredictionEngine:
    """Combined prediction engine: Rule-based + ML ensemble."""

    def __init__(self):
        self.rule_predictor = RuleBasedPredictor()
        self.ml_predictor = MLPredictor()
        self.feature_history = []
        self.label_history = []
        self._retrain_count = 0

    def predict(self, indicators: dict) -> dict:
        from technical_analysis import TechnicalAnalyzer

        rule_result = self.rule_predictor.predict(indicators)
        analyzer = TechnicalAnalyzer()
        feature_vec = analyzer.get_feature_vector(indicators)
        ml_result = self.ml_predictor.predict(feature_vec)

        # Weighted ensemble: rule 60%, ML 40%
        rule_w, ml_w = 0.6, 0.4

        if ml_result["direction"] != 0:
            # Weight by confidence
            rule_score = (rule_w * rule_result["confidence"]
                          if rule_result["direction"] == 1
                          else rule_w * (1 - rule_result["confidence"]))
            ml_score = (ml_w * ml_result["confidence"]
                        if ml_result["direction"] == 1
                        else ml_w * (1 - ml_result["confidence"]))

            combined_up = rule_score + ml_score
            combined_down = (rule_w - rule_score) + (ml_w - ml_score)

            confidence = max(combined_up, combined_down) / (rule_w + ml_w)
            direction = 1 if combined_up > combined_down else -1
        else:
            # ML not ready — use rule only
            confidence = rule_result["confidence"]
            direction = rule_result["direction"]
            ml_w = 0

        agreement = (
            "agree" if rule_result["direction"] == ml_result["direction"]
            else "ml_neutral" if ml_result["direction"] == 0
            else "disagree"
        )

        # Blend signals
        all_signals = rule_result.get("signals", []) + ml_result.get("signals", [])

        return {
            "direction": direction,
            "confidence": float(confidence),
            "agreement": agreement,
            "rule_confidence": float(rule_result["confidence"]),
            "ml_confidence": float(ml_result["confidence"]),
            "rule_weight": rule_w,
            "ml_weight": ml_w,
            "signals": all_signals,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def record_outcome(self, indicators: dict, actual_direction: int):
        """Record outcome and retrain ML periodically."""
        from technical_analysis import TechnicalAnalyzer

        analyzer = TechnicalAnalyzer()
        feature_vec = analyzer.get_feature_vector(indicators)
        if len(feature_vec) > 0:
            self.feature_history.append(feature_vec)
            self.label_history.append(actual_direction)

            # Retrain every 200 samples
            if len(self.feature_history) >= 200 and len(self.feature_history) % 100 == 0:
                self._retrain()
                self._retrain_count += 1

    def _retrain(self):
        """Retrain ML with accumulated data."""
        if len(self.feature_history) < 200:
            return

        # Keep last 2000 samples max
        features = np.array(self.feature_history[-2000:])
        labels = np.array(self.label_history[-2000:])
        self.ml_predictor.train(features, labels)
