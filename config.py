"""BTC 5-Minute Polymarket Prediction System - Configuration"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
MODEL_DIR = BASE_DIR / "models"
LOG_DIR = BASE_DIR / "logs"

for d in [DATA_DIR, MODEL_DIR, LOG_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# === BTC Price Data Sources ===
BINANCE_API = "https://api.binance.com"
OKX_API = "https://www.okx.com"

# === Proxy Configuration ===
HTTP_PROXY = os.environ.get("HTTPS_PROXY", os.environ.get("https_proxy", ""))
HTTP_TIMEOUT = 15

# === Polymarket API ===
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB = "https://clob.polymarket.com"
POLYMARKET_DATA = "https://data-api.polymarket.com"

# === Prediction Settings ===
PREDICTION_WINDOW = 5  # minutes ahead
MIN_CONFIDENCE = 0.55  # minimum confidence to trade
EDGE_THRESHOLD = 0.03  # minimum edge over Polymarket odds (3%)

# === Technical Indicators ===
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BB_PERIOD = 20
BB_STD = 2
EMA_SHORT = 5
EMA_LONG = 20

# === Data Collection ===
KLINE_INTERVAL = "1m"
KLINE_LIMIT = 100
COLLECTION_INTERVAL = 30  # seconds between cycles

# === Web Dashboard ===
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 8765

# === Database ===
DB_PATH = DATA_DIR / "btc_predictor.db"

# === Logging ===
LOG_FILE = LOG_DIR / "predictor.log"

# === Specific Market Config ===
# The Polymarket BTC 5-min market slug pattern
MARKET_SLUG_PATTERN = "btc-updown-5m"
# Resolution source: Chainlink BTC/USD
RESOLUTION_SOURCE = "https://data.chain.link/streams/btc-usd"
