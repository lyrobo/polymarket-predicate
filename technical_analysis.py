"""Technical Analysis Engine - Computes indicators for BTC prediction"""

import numpy as np
import logging
from config import *

logger = logging.getLogger(__name__)


class TechnicalAnalyzer:
    """Compute technical indicators from OHLCV data."""

    def __init__(self):
        self.indicator_cache = {}

    @staticmethod
    def sma(data: np.ndarray, period: int) -> np.ndarray:
        """Simple Moving Average."""
        result = np.full_like(data, np.nan)
        if len(data) < period:
            return result
        # Vectorized SMA using convolution
        kernel = np.ones(period) / period
        valid = np.convolve(data, kernel, mode='valid')
        result[period - 1:] = valid
        return result

    @staticmethod
    def ema(data: np.ndarray, period: int) -> np.ndarray:
        """Exponential Moving Average."""
        result = np.full_like(data, np.nan)
        multiplier = 2 / (period + 1)
        valid_start = period - 1
        if len(data) < period:
            return result
        result[valid_start] = np.mean(data[:period])
        for i in range(valid_start + 1, len(data)):
            result[i] = (data[i] - result[i - 1]) * multiplier + result[i - 1]
        return result

    @staticmethod
    def rsi(data: np.ndarray, period: int = RSI_PERIOD) -> np.ndarray:
        """Relative Strength Index."""
        result = np.full_like(data, np.nan)
        if len(data) < period + 1:
            return result

        deltas = np.diff(data)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])

        if avg_loss == 0:
            result[period] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[period] = 100 - (100 / (1 + rs))

        for i in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            if avg_loss == 0:
                result[i + 1] = 100.0
            else:
                rs = avg_gain / avg_loss
                result[i + 1] = 100 - (100 / (1 + rs))

        return result

    @staticmethod
    def macd(data: np.ndarray, fast: int = MACD_FAST, slow: int = MACD_SLOW,
             signal_period: int = MACD_SIGNAL) -> tuple:
        """MACD indicator - returns (macd_line, signal_line, histogram)."""
        if len(data) < slow:
            return np.full_like(data, np.nan), np.full_like(data, np.nan), np.full_like(data, np.nan)

        ema_fast = TechnicalAnalyzer.ema(data, fast)
        ema_slow = TechnicalAnalyzer.ema(data, slow)
        macd_line = ema_fast - ema_slow
        signal_line = TechnicalAnalyzer.ema(macd_line, signal_period)
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram

    @staticmethod
    def bollinger_bands(data: np.ndarray, period: int = BB_PERIOD,
                        std_mult: int = BB_STD) -> tuple:
        """Bollinger Bands - returns (upper, middle, lower)."""
        middle = TechnicalAnalyzer.sma(data, period)
        rolling_std = np.full_like(data, np.nan)
        for i in range(period - 1, len(data)):
            rolling_std[i] = np.std(data[i - period + 1:i + 1])

        upper = middle + std_mult * rolling_std
        lower = middle - std_mult * rolling_std
        return upper, middle, lower

    @staticmethod
    def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray,
            period: int = 14) -> np.ndarray:
        """Average True Range."""
        result = np.full_like(close, np.nan)
        if len(close) < period + 1:
            return result

        tr = np.zeros(len(close))
        for i in range(1, len(close)):
            tr[i] = max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1])
            )

        result[period] = np.mean(tr[1:period + 1])
        for i in range(period + 1, len(close)):
            result[i] = (result[i - 1] * (period - 1) + tr[i]) / period

        return result

    @staticmethod
    def volume_ratio(volume: np.ndarray, period: int = 20) -> np.ndarray:
        """Volume ratio vs moving average."""
        vol_sma = TechnicalAnalyzer.sma(volume, period)
        with np.errstate(divide='ignore', invalid='ignore'):
            ratio = np.where(vol_sma > 0, volume / vol_sma, 1.0)
        return ratio

    @staticmethod
    def price_position(close: np.ndarray, high: np.ndarray, low: np.ndarray) -> np.ndarray:
        """Price position within high-low range (0-1)."""
        with np.errstate(divide='ignore', invalid='ignore'):
            position = np.where(
                high != low,
                (close - low) / (high - low),
                0.5
            )
        return position

    def compute_all(self, klines: list) -> dict:
        """Compute all technical indicators from kline data.

        Args:
            klines: List of dicts with keys: open, high, low, close, volume, timestamp

        Returns:
            Dictionary of indicator arrays + latest values
        """
        if len(klines) < 30:
            logger.warning(f"Insufficient data for technical analysis: {len(klines)} candles")
            return {}

        closes = np.array([k["close"] for k in klines], dtype=np.float64)
        highs = np.array([k["high"] for k in klines], dtype=np.float64)
        lows = np.array([k["low"] for k in klines], dtype=np.float64)
        volumes = np.array([k["volume"] for k in klines], dtype=np.float64)
        opens = np.array([k["open"] for k in klines], dtype=np.float64)

        result = {}

        # Price-based
        result["close"] = closes
        result["open"] = opens
        result["high"] = highs
        result["low"] = lows
        result["volume"] = volumes

        # Moving averages
        result["sma_5"] = self.sma(closes, 5)
        result["sma_10"] = self.sma(closes, 10)
        result["sma_20"] = self.sma(closes, 20)
        result["ema_5"] = self.ema(closes, EMA_SHORT)
        result["ema_20"] = self.ema(closes, EMA_LONG)

        # RSI
        result["rsi"] = self.rsi(closes, RSI_PERIOD)

        # MACD
        macd_line, signal_line, histogram = self.macd(closes)
        result["macd"] = macd_line
        result["macd_signal"] = signal_line
        result["macd_hist"] = histogram

        # Bollinger Bands
        bb_upper, bb_middle, bb_lower = self.bollinger_bands(closes)
        result["bb_upper"] = bb_upper
        result["bb_middle"] = bb_middle
        result["bb_lower"] = bb_lower

        # Bollinger Band width (relative to middle)
        result["bb_width"] = np.where(
            bb_middle > 0, (bb_upper - bb_lower) / bb_middle, 0.0
        )
        # BB width moving average (20-period)
        result["bb_width_ma"] = self.sma(result["bb_width"], 20)

        # ATR
        result["atr"] = self.atr(highs, lows, closes)
        # ATR moving average (20-period)
        result["atr_ma"] = self.sma(result["atr"], 20)

        # ATR as percentage of price
        atr_pct = np.where(closes > 0, result["atr"] / closes * 100, 0.0)
        result["atr_pct"] = atr_pct
        result["atr_pct_ma"] = self.sma(atr_pct, 20)

        # Volume
        result["volume_ratio"] = self.volume_ratio(volumes)
        # Volume trend (volume ratio SMA)
        result["volume_ratio_ma"] = self.sma(result["volume_ratio"], 10)

        # Price position
        result["price_position"] = self.price_position(closes, highs, lows)

        # Price range (5-period high-low spread as % of price)
        price_range_5 = np.full_like(closes, np.nan)
        for i in range(4, len(closes)):
            h5 = np.max(highs[i - 4:i + 1])
            l5 = np.min(lows[i - 4:i + 1])
            price_range_5[i] = (h5 - l5) / closes[i] * 100 if closes[i] > 0 else 0
        result["price_range_5"] = price_range_5

        # Returns
        result["returns_1"] = np.concatenate([[0], np.diff(closes) / closes[:-1]])
        result["returns_5"] = np.concatenate([[0] * 5, (closes[5:] - closes[:-5]) / closes[:-5]])

        # Return acceleration (2nd derivative: change of returns)
        returns_1 = result["returns_1"]
        returns_accel = np.concatenate([[0], np.diff(returns_1)])
        result["returns_accel"] = returns_accel

        # Volatility (20-period std of returns)
        rolling_vol = np.full_like(closes, np.nan)
        for i in range(19, len(returns_1)):
            rolling_vol[i] = np.std(returns_1[i - 19:i + 1])
        result["volatility_20"] = rolling_vol

        # Volatility regime: ratio of short-term to long-term vol
        vol_short = np.full_like(closes, np.nan)
        for i in range(4, len(returns_1)):
            vol_short[i] = np.std(returns_1[i - 4:i + 1])
        result["vol_ratio"] = np.where(rolling_vol > 0, vol_short / rolling_vol, 1.0)

        # Store latest values for easy access
        result["latest"] = {}
        for key, arr in result.items():
            if key == "latest":
                continue
            if isinstance(arr, np.ndarray) and len(arr) > 0:
                # Use last non-NaN value if possible
                val = arr[-1]
                if np.isnan(val):
                    valid = arr[~np.isnan(arr)]
                    val = valid[-1] if len(valid) > 0 else 0
                result["latest"][key] = val

        return result

    def get_feature_vector(self, indicators: dict) -> np.ndarray:
        """Extract enhanced feature vector from indicators for ML model.

        Returns 22 features capturing momentum, volatility, and regime.
        """
        latest = indicators.get("latest", {})
        closes = indicators.get("close", np.array([]))

        if len(closes) < 5:
            return np.array([])

        current_price = closes[-1]
        if current_price <= 0:
            return np.array([])

        # Helper: safe value with default
        def sv(key, default=0.0):
            v = latest.get(key, default)
            return default if v is None or np.isnan(v) else float(v)

        features = [
            # === Momentum Features (4) ===
            sv("returns_1", 0),                              # 0: 1-min return
            sv("returns_5", 0),                              # 1: 5-min return
            sv("returns_accel", 0),                          # 2: return acceleration
            sv("close") / (sv("ema_5", current_price) or current_price) - 1,  # 3: vs EMA5

            # === Trend Features (5) ===
            sv("close") / (sv("sma_20", current_price) or current_price) - 1,  # 4: vs SMA20
            (sv("ema_5") - sv("ema_20")) / (current_price or 1),  # 5: EMA crossover
            sv("macd_hist") / (current_price or 1) * 10000,   # 6: MACD hist normalized
            sv("rsi", 50) / 100 - 0.5,                        # 7: RSI (centered at 0)
            self._bb_position(latest) - 0.5,                   # 8: BB position (centered)

            # === Volatility Features (5) ===
            sv("atr_pct", 0) / 100,                           # 9: ATR as % of price
            sv("atr_pct") / (sv("atr_pct_ma", sv("atr_pct")) or 0.001) - 1,  # 10: ATR expansion
            sv("bb_width") / (sv("bb_width_ma", sv("bb_width")) or 0.001) - 1,  # 11: BB squeeze
            sv("price_range_5", 0) / 100,                     # 12: 5-bar range %
            sv("vol_ratio", 1.0) - 1.0,                       # 13: volatility regime

            # === Volume Features (3) ===
            sv("volume_ratio", 1.0) - 1.0,                    # 14: volume anomaly
            sv("volume_ratio") / (sv("volume_ratio_ma", 1.0) or 1.0) - 1,  # 15: volume trend
            sv("price_position", 0.5) - 0.5,                  # 16: intra-bar position

            # === Regime Features (5) ===
            sv("volatility_20", 0) * 100,                     # 17: raw volatility
            sv("close") / (sv("sma_5", current_price) or current_price) - 1,  # 18: vs SMA5
            sv("close") / (sv("sma_10", current_price) or current_price) - 1,  # 19: vs SMA10
            (sv("macd") - sv("macd_signal")) / (current_price or 1) * 10000,  # 20: MACD diff
            (closes[-1] - closes[-5]) / (np.max(closes[-5:]) - np.min(closes[-5:]) + 1e-10) if len(closes) >= 5 else 0,  # 21: relative position in 5-bar range
        ]

        return np.array(features, dtype=np.float64)

    @staticmethod
    def _bb_position(latest: dict) -> float:
        """Bollinger Band position (0=lower, 0.5=middle, 1=upper)."""
        close = latest.get("close", 0)
        upper = latest.get("bb_upper", 0)
        lower = latest.get("bb_lower", 0)
        if upper and lower and upper != lower:
            return (close - lower) / (upper - lower)
        return 0.5
