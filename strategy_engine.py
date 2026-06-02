"""Strategy Engine - Full prediction pipeline for BTC 5-min Polymarket"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from config import *
from data_collector import DataCollector
from technical_analysis import TechnicalAnalyzer
from prediction_engine import PredictionEngine
from polymarket_client import PolymarketClient, compare_prediction_vs_market

logger = logging.getLogger(__name__)


class StrategyEngine:
    """Orchestrates the full prediction pipeline."""

    def __init__(self):
        self.collector = DataCollector()
        self.analyzer = TechnicalAnalyzer()
        self.predictor = PredictionEngine()
        self.polymarket = PolymarketClient()
        self._init_db()

        self.last_prediction = None
        self.last_signal = None
        self.signal_log = []

    def _init_db(self):
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, price REAL, direction INTEGER,
                confidence REAL, rule_conf REAL, ml_conf REAL,
                agreement TEXT, signals TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, price REAL, signal_type TEXT,
                confidence REAL, edge REAL, market_up REAL, market_down REAL,
                action TEXT, recommendation TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, price REAL, volume REAL
            )
        """)
        conn.commit()
        conn.close()

    def run_cycle(self) -> dict:
        """One full prediction cycle."""
        t0 = time.time()
        result = {"timestamp": datetime.now(timezone.utc).isoformat(), "success": False, "errors": []}

        # 1. Fetch data
        klines = self.collector.get_klines()
        price = self.collector.get_price()
        if not klines:
            result["errors"].append("No kline data")
            return result
        if not price:
            price = klines[-1]["close"]

        result["price"] = price
        result["kline_count"] = len(klines)
        self._store_price(price, klines[-1]["volume"])

        # 2. Technical analysis
        indicators = self.analyzer.compute_all(klines)
        if not indicators:
            result["errors"].append("TA failed")
            return result

        result["indicators"] = {
            "rsi": round(indicators["latest"].get("rsi", 0), 2),
            "macd_hist": round(indicators["latest"].get("macd_hist", 0), 4),
            "volume_ratio": round(indicators["latest"].get("volume_ratio", 0), 2),
            "ema_5": round(indicators["latest"].get("ema_5", 0), 2),
            "ema_20": round(indicators["latest"].get("ema_20", 0), 2),
        }

        # 3. Prediction
        prediction = self.predictor.predict(indicators)
        self.last_prediction = prediction
        result["prediction"] = prediction
        self._store_prediction(prediction, price)

        # 4. Polymarket comparison
        try:
            market = self.polymarket.get_best_market()
            if market:
                edge = compare_prediction_vs_market(prediction, market)
                result["polymarket"] = {
                    "market": market,
                    "edge_analysis": edge,
                }
            else:
                result["polymarket"] = {"error": "No active BTC 5-min market found"}
        except Exception as e:
            logger.warning(f"Polymarket check failed: {e}")
            result["polymarket"] = {"error": str(e)}

        # 5. Signal
        signal = self._generate_signal(prediction, result.get("polymarket", {}))
        result["signal"] = signal
        self.last_signal = signal
        self.signal_log.append(signal)
        if len(self.signal_log) > 500:
            self.signal_log = self.signal_log[-250:]
        self._store_signal(signal)

        # 6. ML training feedback
        if len(klines) >= 2:
            actual = 1 if klines[-1]["close"] > klines[-2]["close"] else -1
            self.predictor.record_outcome(indicators, actual)

        result["cycle_time_ms"] = round((time.time() - t0) * 1000, 1)
        result["success"] = True

        logger.info(
            f"Cycle: ${price:,.2f} | "
            f"{'📈' if prediction['direction']==1 else '📉'} "
            f"{prediction['confidence']*100:.1f}% | "
            f"signal={signal['type']} | "
            f"{result['cycle_time_ms']:.0f}ms"
        )
        return result

    def _generate_signal(self, pred: dict, pm: dict) -> dict:
        conf = pred["confidence"]
        direction = pred["direction"]

        if conf >= MIN_CONFIDENCE:
            sig_type = "BUY" if direction == 1 else "SELL"
        elif conf >= 0.45:
            sig_type = "WEAK_BUY" if direction == 1 else "WEAK_SELL"
        else:
            sig_type = "HOLD"

        edge_info = pm.get("edge_analysis", {})
        return {
            "type": sig_type,
            "direction": "UP" if direction == 1 else "DOWN",
            "confidence": float(conf),
            "polymarket_action": edge_info.get("action", "HOLD"),
            "edge": float(edge_info.get("edge", 0)),
            "recommendation": edge_info.get("recommendation", ""),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _store_prediction(self, pred: dict, price: float):
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute(
                "INSERT INTO predictions (timestamp, price, direction, confidence, rule_conf, ml_conf, agreement, signals) VALUES (?,?,?,?,?,?,?,?)",
                (pred["timestamp"], price, pred["direction"], pred["confidence"],
                 pred.get("rule_confidence", 0), pred.get("ml_confidence", 0),
                 pred.get("agreement", ""), json.dumps(pred.get("signals", []))),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"DB error: {e}")

    def _store_signal(self, signal: dict):
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute(
                "INSERT INTO signals (timestamp, price, signal_type, confidence, edge, market_up, market_down, action, recommendation) VALUES (?,?,?,?,?,?,?,?,?)",
                (signal["timestamp"], self.collector.get_price() or 0, signal["type"],
                 signal["confidence"], signal.get("edge", 0), 0, 0,
                 signal.get("polymarket_action", "HOLD"), signal.get("recommendation", "")),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"DB error: {e}")

    def _store_price(self, price: float, volume: float):
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute(
                "INSERT INTO price_history (timestamp, price, volume) VALUES (?,?,?)",
                (datetime.now(timezone.utc).isoformat(), price, volume),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"DB error: {e}")

    def get_stats(self) -> dict:
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            stats = {
                "total_predictions": conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0],
                "total_signals": conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0],
                "recent_predictions": [dict(r) for r in conn.execute("SELECT * FROM predictions ORDER BY id DESC LIMIT 20").fetchall()],
                "recent_prices": [dict(p) for p in conn.execute("SELECT * FROM price_history ORDER BY id DESC LIMIT 100").fetchall()],
                "last_prediction": self.last_prediction,
                "last_signal": self.last_signal,
                "signal_log": self.signal_log[-20:],
            }
            conn.close()
            return stats
        except Exception as e:
            logger.warning(f"Stats error: {e}")
            return {}
