"""Improved Prediction Engine - Optimized for 5-minute BTC prediction"""

import json
import logging
import numpy as np
from pathlib import Path
from datetime import datetime, timezone
from config import *

logger = logging.getLogger(__name__)


class ImprovedRuleBasedPredictor:
    """Improved rule-based prediction with shorter-term indicators."""

    def predict(self, indicators: dict) -> dict:
        latest = indicators.get("latest", {})
        signals = []
        score = 0.0

        # 1. Short-term momentum (more relevant for 5min)
        ret_1 = latest.get("returns_1", 0)
        ret_3 = latest.get("returns_3", 0)
        ret_5 = latest.get("returns_5", 0)
        
        if ret_1 is not None and not np.isnan(ret_1):
            if ret_1 > 0.001: score += 0.08; signals.append(f"1m momentum +{ret_1*100:.2f}%")
            elif ret_1 < -0.001: score -= 0.08; signals.append(f"1m momentum {ret_1*100:.2f}%")
        
        if ret_3 is not None and not np.isnan(ret_3):
            if ret_3 > 0.003: score += 0.10; signals.append("3m uptrend")
            elif ret_3 < -0.003: score -= 0.10; signals.append("3m downtrend")

        # 2. Volume-weighted price action
        vol_ratio = latest.get("volume_ratio", 1)
        price_position = latest.get("price_position", 0.5)
        
        if vol_ratio > 1.2 and price_position > 0.7:
            score -= 0.08  # High volume at upper BB → reversal likely
            signals.append("High vol + upper BB → reversal")
        elif vol_ratio > 1.2 and price_position < 0.3:
            score += 0.08  # High volume at lower BB → bounce likely
            signals.append("High vol + lower BB → bounce")

        # 3. ATR-based volatility filter
        atr = latest.get("atr", 0)
        if atr is not None and not np.isnan(atr) and atr > 0:
            # Low ATR = consolidation = mean reversion more likely
            # High ATR = trending = momentum more reliable
            if atr < 50:  # Low volatility
                # Mean reversion strategy
                if price_position > 0.7: score += 0.06; signals.append("Low vol + high BB → mean reversion")
                elif price_position < 0.3: score -= 0.06; signals.append("Low vol + low BB → mean reversion")
            else:  # High volatility
                # Momentum strategy
                if ret_5 > 0.005: score += 0.12; signals.append("High vol + uptrend → momentum")
                elif ret_5 < -0.005: score -= 0.12; signals.append("High vol + downtrend → momentum")

        # 4. MACD on shorter timeframe
        macd_hist = latest.get("macd_hist", 0)
        if macd_hist is not None and not np.isnan(macd_hist):
            if abs(macd_hist) > 5:  # Significant MACD signal
                if macd_hist > 0: score += 0.08; signals.append(f"MACD +{macd_hist:.1f}")
                else: score -= 0.08; signals.append(f"MACD {macd_hist:.1f}")

        # 5. RSI divergence (more sensitive)
        rsi = latest.get("rsi", 50)
        if rsi is not None and not np.isnan(rsi):
            if rsi < 25: score += 0.12; signals.append(f"RSI deeply oversold ({rsi:.1f})")
            elif rsi > 75: score -= 0.12; signals.append(f"RSI deeply overbought ({rsi:.1f})")
            elif rsi < 35: score += 0.05
            elif rsi > 65: score -= 0.05

        confidence = max(0.0, min(1.0, 0.5 + score / 2))
        direction = 1 if confidence > 0.5 else -1

        return {
            "method": "improved_rule", "direction": direction, "confidence": float(confidence),
            "score": float(score), "signals": signals,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


class ImprovedMLPredictor:
    """Improved ML with better feature engineering."""

    def __init__(self, n_estimators=100):
        self.n_estimators = n_estimators
        self.trees = []
        self.is_trained = False
        self.model_path = MODEL_DIR / "improved_rf_model.json"
        self._load_model()

    def _load_model(self):
        if self.model_path.exists():
            try:
                with open(self.model_path) as f:
                    data = json.load(f)
                self.trees = data["trees"]
                self.is_trained = True
                logger.info(f"Loaded improved ML model: {len(self.trees)} stumps")
            except Exception as e:
                logger.warning(f"Load model failed: {e}")

    def _save_model(self):
        try:
            with open(self.model_path, "w") as f:
                json.dump({"trees": self.trees, "trained_at": datetime.now(timezone.utc).isoformat()}, f)
        except Exception as e:
            logger.warning(f"Save model failed: {e}")

    def train(self, features: np.ndarray, labels: np.ndarray):
        if len(features) < 50:
            logger.warning(f"Insufficient training data: {len(features)}")
            return

        n_features = features.shape[1]
        n_subset = max(2, int(np.sqrt(n_features)))
        self.trees = []

        for i in range(self.n_estimators):
            # Bootstrap sampling
            indices = np.random.choice(len(features), size=len(features), replace=True)
            
            # Random feature subset
            feat_subset = np.random.choice(n_features, size=min(n_subset, n_features), replace=False)

            best_feat, best_thresh, best_score = None, None, float('inf')
            best_left, best_right = 0, 0

            for fi in feat_subset:
                if fi >= features.shape[1]: continue
                vals = features[indices, fi]
                if np.all(vals == vals[0]): continue
                
                # More threshold candidates
                for thresh in np.percentile(vals, [20, 40, 50, 60, 80]):
                    left = vals <= thresh
                    right = ~left
                    if left.sum() == 0 or right.sum() == 0: continue
                    
                    l_val, r_val = np.mean(labels[left]), np.mean(labels[right])
                    score = left.sum() * np.var(labels[left]) + right.sum() * np.var(labels[right])
                    
                    if score < best_score:
                        best_score, best_feat, best_thresh = score, int(fi), float(thresh)
                        best_left, best_right = float(l_val), float(r_val)

            if best_feat is not None:
                self.trees.append({
                    "feature": best_feat, "threshold": best_thresh,
                    "left_val": best_left, "right_val": best_right,
                })

        self.is_trained = True
        self._save_model()
        logger.info(f"Trained {len(self.trees)} stumps on {len(features)} samples")

    def predict(self, feature_vector: np.ndarray) -> dict:
        if not self.is_trained or not self.trees:
            return {"method": "ml", "direction": 0, "confidence": 0.5,
                    "signals": ["ML not trained yet"],
                    "timestamp": datetime.now(timezone.utc).isoformat()}

        votes = []
        for tree in self.trees:
            fi = tree["feature"]
            if fi is None or fi >= len(feature_vector):
                votes.append(0); continue
            val = feature_vector[fi]
            if np.isnan(val): votes.append(0)
            elif val <= tree["threshold"]: votes.append(tree["left_val"])
            else: votes.append(tree["right_val"])

        avg = np.mean(votes)
        confidence = max(0.0, min(1.0, 0.5 + avg / 2))
        return {
            "method": "ml", "direction": 1 if confidence > 0.5 else -1,
            "confidence": float(confidence), "n_trees": len(self.trees),
            "avg_vote": float(avg),
            "signals": [f"ML: {len(self.trees)} trees, vote={avg:.3f}"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


class ImprovedPredictionEngine:
    """Combined prediction engine with improved algorithms."""

    def __init__(self):
        self.rule_predictor = ImprovedRuleBasedPredictor()
        self.ml_predictor = ImprovedMLPredictor()
        self.feature_history = []
        self.label_history = []

    def predict(self, indicators: dict) -> dict:
        from technical_analysis import TechnicalAnalyzer
        rule_result = self.rule_predictor.predict(indicators)
        analyzer = TechnicalAnalyzer()
        feature_vec = analyzer.get_feature_vector(indicators)
        ml_result = self.ml_predictor.predict(feature_vec)

        # Adaptive weighting based on agreement
        if rule_result["direction"] == ml_result["direction"] and ml_result["direction"] != 0:
            # Both agree → higher confidence
            rule_w, ml_w = 0.6, 0.4
        else:
            # Disagree → trust rule-based more
            rule_w, ml_w = 0.75, 0.25

        if ml_result["direction"] != 0:
            combined = rule_w * rule_result["confidence"] + ml_w * ml_result["confidence"]
        else:
            combined = rule_result["confidence"]
            ml_w = 0

        direction = 1 if combined > 0.5 else -1
        agreement = ("agree" if rule_result["direction"] == ml_result["direction"] or ml_result["direction"] == 0
                     else "disagree" if ml_result["direction"] != 0 else "ml_untrained")

        return {
            "direction": direction, "confidence": float(combined),
            "agreement": agreement,
            "rule_confidence": float(rule_result["confidence"]),
            "ml_confidence": float(ml_result["confidence"]),
            "rule_weight": rule_w, "ml_weight": ml_w,
            "signals": rule_result.get("signals", []) + ml_result.get("signals", []),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def record_outcome(self, indicators: dict, actual_direction: int):
        from technical_analysis import TechnicalAnalyzer
        analyzer = TechnicalAnalyzer()
        feature_vec = analyzer.get_feature_vector(indicators)
        if len(feature_vec) > 0:
            self.feature_history.append(feature_vec)
            self.label_history.append(actual_direction)
            if len(self.feature_history) >= 100:
                feats = np.array(self.feature_history[-500:])
                labs = np.array(self.label_history[-500:])
                self.ml_predictor.train(feats, labs)
