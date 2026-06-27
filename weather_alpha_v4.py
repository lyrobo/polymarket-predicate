#!/usr/bin/env python3
"""
🌦 Weather Alpha v4 — WeatherHK-Inspired Multi-Bin Quant System

Key improvements over v3 (inspired by @weatherhk):
  - Multi-bin strategy: bet on ALL temperature bins per city/day, not just the best one
  - Fewer cities, deeper bets: focus on 10-15 highest-data-quality cities  
  - True Kelly sizing: variable position size scales with edge × confidence
  - Multi-variable: temp_max + temp_min + precipitation markets
  - Higher position cap: 20 instead of 10
  - Direct market discovery: search by city name, not just whale-follow

WeatherHK's edge:
  - Only bets HK + SZ (2 cities)
  - Multi-bin: builds probability distribution, bets all mispriced bins
  - Kelly-style sizing: $2-$144 range based on conviction
  - Multi-variable: temp_max, temp_min, precipitation

Usage:
  python3 weather_alpha_v4.py                    # scan once, print signals
  python3 weather_alpha_v4.py --live             # continuous scan + DB
  python3 weather_alpha_v4.py --live --capital 500
"""

import os, sys, json, time, signal, logging, argparse, sqlite3, subprocess, re, math
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple
from collections import defaultdict, OrderedDict

# ── CLOB Live Trading via Node.js bridge ──
import subprocess as _sp

def place_clob_order(token_id, price, size, side="BUY"):
    """Place order via Node.js clob_bridge.js."""
    if not token_id:
        return {"success": False, "error": "no token_id"}
    try:
        node = "/root/.hermes/node/bin/node"
        script = str(BASE_DIR / "clob_bridge.js")
        cmd = [node, script, "order", side, token_id, str(price), str(size)]
        r = _sp.run(cmd, capture_output=True, text=True, timeout=20, cwd=str(BASE_DIR))
        if r.returncode != 0:
            return {"success": False, "error": r.stderr[:100]}
        lines = [l for l in r.stdout.strip().split("\n") if l.strip().startswith("{")]
        result = json.loads(lines[-1]) if lines else {}
        if result.get("success"):
            return {"success": True, "order_id": result.get("orderID", ""), "resp": result}
        return {"success": False, "error": result.get("errorMsg", "unknown")[:200]}
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"
for d in [LOG_DIR, DATA_DIR]: d.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "weather_alpha_v4.db"
GAMMA = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
ENSEMBLE_API = "https://ensemble-api.open-meteo.com/v1/ensemble"
ENSEMBLE_MODELS = "icon_seamless"

# ── Focus cities: WeatherHK's territory + global weather hubs ──
# Priority ordered: HK/SZ first (WeatherHK proven), then alpha cities
FOCUS_CITIES: Dict[str, Tuple[float, float]] = OrderedDict([
    # WeatherHK's domain — proven edge
    ("hong-kong",      (22.30,  114.20)),
    ("shenzhen",       (22.54,  114.06)),
    # Asia — high data quality markets
    ("tokyo",          (35.68,  139.76)),
    ("seoul",          (37.57,  126.98)),
    ("shanghai",       (31.23,  121.47)),
    ("chongqing",      (29.56,  106.55)),
    ("manila",         (14.60,  120.98)),
    ("singapore",      (1.35,   103.82)),
    # Americas — deep liquidity
    ("new-york",       (40.71,  -74.01)),
    ("chicago",        (41.88,  -87.63)),
    ("los-angeles",    (34.05,  -118.24)),
    ("houston",        (29.76,  -95.37)),
    ("miami",          (25.76,  -80.19)),
    ("toronto",        (43.65,  -79.38)),
    ("buenos-aires",   (-34.60, -58.38)),
    ("mexico-city",    (19.43,  -99.13)),
    # Europe
    ("london",         (51.51,  -0.13)),
    ("paris",          (48.85,  2.35)),
    ("helsinki",       (60.17,  24.94)),
])

# ── Config ─────────────────────────────────────────────────────
FORECAST_DAYS = 3
POLL_INTERVAL = 600

# ── Wunderground API (Polymarket resolution source) ──
WU_API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"
WU_API_BASE = "https://api.weather.com/v1/location"

# Airport ICAO codes matching Polymarket resolution stations
AIRPORT_STATIONS = {
    "hong-kong":     "VHHH:9:HK",
    "shenzhen":      "ZGSZ:9:CN",
    "tokyo":         "RJTT:9:JP",
    "seoul":         "RKSI:9:KR",
    "shanghai":      "ZSPD:9:CN",
    "chongqing":     "ZUCK:9:CN",
    "manila":        "RPLL:9:PH",
    "singapore":     "WSSS:9:SG",
    "new-york":      "KJFK:9:US",
    "chicago":       "KORD:9:US",
    "los-angeles":   "KLAX:9:US",
    "houston":       "KIAH:9:US",
    "miami":         "KMIA:9:US",
    "toronto":       "CYYZ:9:CA",
    "buenos-aires":  "SAEZ:9:AR",
    "mexico-city":   "MMMX:9:MX",
    "london":        "EGLL:9:GB",
    "paris":         "LFPG:9:FR",
    "helsinki":      "EFHK:9:FI",
}
MIN_EDGE = 0.05            # 5% minimum edge
MAX_EDGE = 0.98            # cap at 98% (avoid fake 100% edges from thin markets)
MIN_CONFIDENCE = 55        # lowered — we want more signals per city
MIN_MARKET_PROB = 0.02     # ignore markets priced below 2% or above 98% (thin liquidity)
DEFAULT_CAPITAL = 200.0
MAX_POSITION_PCT = 0.08    # 8% max per position (was 5%)
KELLY_FRACTION = 0.30      # 30% Kelly (was 25%)
MAX_TOTAL_POSITIONS = 20   # was 10
MIN_AVAILABLE = 50.0       # keep $50 buffer

HKT = timezone(timedelta(hours=8))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "weather_alpha_v4.log"), logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("weather_alpha_v4")


# ═══════════════════════════════════════════════════════════════
# PROBABILITY ENGINE
# ═══════════════════════════════════════════════════════════════

class NormCDF:
    @staticmethod
    def cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
    
    @staticmethod
    def sf(x: float) -> float:
        return 0.5 * math.erfc(x / math.sqrt(2.0))


class ProbabilityEngine:
    def __init__(self, sigma: float = 1.5):
        self.sigma = sigma
    
    def prob_above(self, forecast: float, threshold: float) -> float:
        z = (threshold - forecast) / self.sigma if self.sigma > 0 else 0
        return round(NormCDF.sf(z), 6)
    
    def prob_below(self, forecast: float, threshold: float) -> float:
        z = (threshold - forecast) / self.sigma if self.sigma > 0 else 0
        return round(NormCDF.cdf(z), 6)
    
    def prob_between(self, forecast: float, lo: float, hi: float) -> float:
        return round(self.prob_above(forecast, lo) - self.prob_above(forecast, hi), 6)


# ═══════════════════════════════════════════════════════════════
# CONFIDENCE ENGINE (same as v3)
# ═══════════════════════════════════════════════════════════════

class ConfidenceEngine:
    @staticmethod
    def agreement_score(sigma: float, n_members: int = 40) -> float:
        if sigma <= 0:
            return 95.0
        score = 100 - sigma * 30
        member_bonus = min(10, (n_members - 10) * 0.3) if n_members > 10 else 0
        return max(0, min(100, score + member_bonus))
    
    @staticmethod
    def distance_score(forecast: float, threshold: float) -> float:
        gap = abs(forecast - threshold)
        if gap <= 0.3: return 20.0
        if gap <= 0.5: return 40.0
        if gap <= 1.0: return 60.0
        if gap <= 2.0: return 80.0
        return 95.0
    
    @staticmethod
    def time_score(days_to_resolution: int) -> float:
        if days_to_resolution <= 0: return 95.0
        if days_to_resolution == 1: return 85.0
        if days_to_resolution == 2: return 65.0
        return max(10, 50 - (days_to_resolution - 2) * 10)
    
    @classmethod
    def compute(cls, sigma: float, n_members: int, forecast: float,
                threshold: float, days_to_res: int = 1) -> int:
        a = cls.agreement_score(sigma, n_members)
        d = cls.distance_score(forecast, threshold)
        t = cls.time_score(days_to_res)
        return int(a * 0.35 + d * 0.35 + t * 0.3)


# ═══════════════════════════════════════════════════════════════
# PORTFOLIO MANAGER — True Kelly with edge×confidence scaling
# ═══════════════════════════════════════════════════════════════

class PortfolioManager:
    """WeatherHK-style: variable position size based on edge and confidence."""
    
    def __init__(self, capital: float, max_pct: float = MAX_POSITION_PCT,
                 kelly_frac: float = KELLY_FRACTION):
        self.capital = capital
        self.max_pct = max_pct
        self.kelly_frac = kelly_frac
        self.positions: Dict[str, dict] = {}
    
    def kelly_bet(self, edge: float, market_prob: float) -> float:
        """Full Kelly fraction."""
        if market_prob <= 0 or market_prob >= 1:
            return 0
        denom = max(market_prob, 1 - market_prob)
        kelly = abs(edge) / denom
        return min(kelly * self.kelly_frac, 1.0)
    
    def size_position(self, edge: float, market_prob: float, confidence: int) -> float:
        """WeatherHK-style sizing: edge-driven with confidence multiplier.
        
        Instead of a flat confidence multiplier, we use the product:
          size = capital * kelly * (conf/100)^1.5
        
        This makes high-edge + high-confidence positions much larger,
        while low-edge positions stay small.
        """
        if abs(edge) < MIN_EDGE or confidence < MIN_CONFIDENCE:
            return 0
        
        kelly_pct = self.kelly_bet(edge, market_prob)
        
        # Edge scaling: edge=0.05 → 0.0, edge=0.50 → 1.0
        edge_scale = (abs(edge) - MIN_EDGE) / (0.50 - MIN_EDGE)
        edge_scale = max(0, min(1, edge_scale))
        
        # Confidence scaling: non-linear boost for high confidence
        # conf=55 → 0.38, conf=75 → 0.65, conf=95 → 0.93
        conf_scale = (confidence / 100) ** 1.5
        
        position_pct = kelly_pct * conf_scale * (0.3 + 0.7 * edge_scale)
        position_pct = min(position_pct, self.max_pct)
        
        return round(self.capital * position_pct, 2)
    
    def signal_tier(self, edge: float, confidence: int) -> str:
        abs_edge = abs(edge)
        if abs_edge >= 0.15 and confidence >= 85: return "A"
        if abs_edge >= 0.10 and confidence >= 75: return "B"
        if abs_edge >= MIN_EDGE and confidence >= MIN_CONFIDENCE: return "C"
        return "D"


# ═══════════════════════════════════════════════════════════════
# DATA FETCHING
# ═══════════════════════════════════════════════════════════════

def curl(url: str) -> Optional[dict]:
    try:
        r = subprocess.run(
            ["/usr/bin/env", "-u", "SSL_CERT_FILE", "-u", "REQUESTS_CA_BUNDLE",
             "/usr/bin/curl", "-s", "--connect-timeout", "4", "--max-time", "12", url],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


def curl_list(url: str) -> list:
    d = curl(url)
    return d if isinstance(d, list) else []


_forecast_cache = None
_forecast_cache_ts = 0

def fetch_all_forecasts() -> Dict[str, List[dict]]:
    """Fetch Wunderground daily forecasts for focus cities. Cached for 30 min.
    
    Wunderground is Polymarket's official resolution source for weather markets.
    Uses airport station data (not gridded reanalysis) for exact PM matching.
    """
    global _forecast_cache, _forecast_cache_ts
    now = time.time()
    if _forecast_cache is not None and (now - _forecast_cache_ts) < 1800:
        logger.info("📡 Using cached forecasts (%d min old)", int((now - _forecast_cache_ts) / 60))
        return _forecast_cache
    
    logger.info("📡 Fetching Wunderground forecasts for %d cities...", len(FOCUS_CITIES))
    forecasts = defaultdict(list)
    
    DEFAULT_SIGMA = 0.5
    DEFAULT_N_MEMBERS = 1
    
    for city, (lat, lon) in FOCUS_CITIES.items():
        station = AIRPORT_STATIONS.get(city)
        if not station:
            logger.warning("   ⚠️ No station for %s, skipping", city)
            continue
        
        url = (f"{WU_API_BASE}/{station}/forecast/daily/{FORECAST_DAYS}day.json"
               f"?apiKey={WU_API_KEY}&units=m")
        data = curl(url)
        
        if not data or "forecasts" not in data:
            logger.warning("   ⚠️ WU API failed for %s (%s)", city, station)
            continue
        
        for fc in data["forecasts"]:
            date_str = fc.get("fcst_valid_local", "")[:10]
            if not date_str:
                continue
            
            max_temp = fc.get("max_temp")
            min_temp = fc.get("min_temp")
            
            if max_temp is not None:
                forecasts[date_str].append({
                    "city": city,
                    "date": date_str,
                    "variable": "temp_max",
                    "value": float(max_temp),
                    "sigma": DEFAULT_SIGMA,
                    "n_members": DEFAULT_N_MEMBERS,
                })
            
            if min_temp is not None:
                forecasts[date_str].append({
                    "city": city,
                    "date": date_str,
                    "variable": "temp_min",
                    "value": float(min_temp),
                    "sigma": DEFAULT_SIGMA,
                    "n_members": DEFAULT_N_MEMBERS,
                })
        
        time.sleep(0.3)  # rate limit
    
    _forecast_cache = dict(forecasts)
    _forecast_cache_ts = time.time()
    logger.info("✅ Got forecasts for %d city-dates", sum(len(v) for v in forecasts.values()))
    return _forecast_cache
    
    logger.info("📡 Fetching ensemble forecasts for %d cities...", len(FOCUS_CITIES))
    forecasts = defaultdict(list)
    
    REGULAR_API = "https://api.open-meteo.com/v1/forecast"
    DEFAULT_SIGMA = 0.5
    DEFAULT_N_MEMBERS = 1
    
    for city, (lat, lon) in FOCUS_CITIES.items():
        data = None
        source = ""
        
        # Try ensemble API first (real sigma for confidence)
        url = (f"{ENSEMBLE_API}?latitude={lat}&longitude={lon}"
               f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
               f"&models={ENSEMBLE_MODELS}"
               f"&forecast_days={FORECAST_DAYS}&timezone=auto")
        data = curl(url)
        if data and "daily" in data:
            source = "ensemble"
        else:
            # Fallback to regular API
            url = (f"{REGULAR_API}?latitude={lat}&longitude={lon}"
                   f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
                   f"&forecast_days={FORECAST_DAYS}&timezone=auto")
            data = curl(url)
            if data and "daily" in data:
                source = "regular"
        
        if not data or "daily" not in data:
            continue
        
        daily = data["daily"]
        times = daily.get("time", [])
        
        if source == "ensemble":
            # Regular API: no ensemble, use default sigma
            var_map = {
                "temp_max": "temperature_2m_max",
                "temp_min": "temperature_2m_min", 
                "precip": "precipitation_sum",
            }
            for var_name, var_field in var_map.items():
                values = daily.get(var_field, [])
                if not values:
                    continue
                for i, val in enumerate(values):
                    if i >= len(times) or val is None:
                        continue
                    forecasts[times[i]].append({
                        "city": city,
                        "date": times[i],
                        "variable": var_name,
                        "value": round(float(val), 1),
                        "sigma": DEFAULT_SIGMA,
                        "n_members": DEFAULT_N_MEMBERS,
                    })
        else:
            # Ensemble API: compute sigma from members
            for var_name, var_field in [("temp_max", "temperature_2m_max"),
                                         ("temp_min", "temperature_2m_min"),
                                         ("precip", "precipitation_sum")]:
                members_by_day: Dict[str, List[float]] = defaultdict(list)
                for key, values in daily.items():
                    if var_field not in key or not values:
                        continue
                    for i, val in enumerate(values):
                        if i >= len(times) or val is None:
                            continue
                        members_by_day[times[i]].append(float(val))
                
                for date_str, member_vals in members_by_day.items():
                    if len(member_vals) < 5:
                        continue
                    mean_val = sum(member_vals) / len(member_vals)
                    variance = sum((v - mean_val) ** 2 for v in member_vals) / len(member_vals)
                    sigma = math.sqrt(variance) if variance > 0.01 else 0.5
                    
                    forecasts[date_str].append({
                        "city": city,
                        "date": date_str,
                        "variable": var_name,
                        "value": round(mean_val, 1),
                        "sigma": round(sigma, 2),
                        "n_members": len(member_vals),
                    })
    
    _forecast_cache = dict(forecasts)
    _forecast_cache_ts = time.time()
    return _forecast_cache


def discover_markets() -> List[dict]:
    """Multi-source market discovery: whale activity + direct city search.
    
    Strategy:
      1. Follow weather whale for his market slugs (proven edge)
      2. Direct search for city names to find ALL markets (not just whale's)
    """
    logger.info("🔍 Discovering weather markets...")
    slugs_seen = set()
    markets = []
    
    # ── Source 1: Weather whale activity ──
    whale_wallets = [
        "0x6a8d1709bfb718d8555d315a983c4816278350f9",  # main weather whale
        "0x488c725253fc21c7a9ca812030dc2f6343f98c1c",  # WeatherHK
    ]
    
    for whale in whale_wallets:
        for offset in [0, 50, 100]:
            data = curl_list(f"{DATA_API}/activity?user={whale}&limit=50&offset={offset}&type=TRADE")
            if not data:
                break
            for a in data:
                slug = a.get("slug", "")
                if slug and slug not in slugs_seen:
                    slugs_seen.add(slug)
    
    logger.info("   %d unique slugs from whale activity", len(slugs_seen))
    
    # ── Source 2: Direct city name search ──
    for city_name in ["hong-kong", "shenzhen", "tokyo", "seoul", "shanghai", 
                       "chongqing", "new-york", "chicago", "london", "paris",
                       "los-angeles", "houston", "buenos-aires", "toronto"]:
        try:
            # Search markets by title
            r = curl(f"{GAMMA}/markets?title={city_name.replace('-', '+')}&active=true&limit=30")
            if r and isinstance(r, list):
                for m in r:
                    slug = m.get("slug", "")
                    if slug and slug not in slugs_seen:
                        slugs_seen.add(slug)
        except:
            pass
    
    logger.info("   %d total unique slugs after direct search", len(slugs_seen))
    
    # ── Fetch market details ──
    for slug in list(slugs_seen)[:200]:
        m = curl(f"{GAMMA}/markets?slug={slug}")
        if not m or not isinstance(m, list) or not m:
            continue
        mkt = m[0]
        if mkt.get("closed") or not mkt.get("active"):
            continue
        prices = mkt.get("outcomePrices", "[]")
        if isinstance(prices, str):
            prices = json.loads(prices)
        markets.append({
            "slug": slug,
            "question": mkt.get("question", ""),
            "price_yes": float(prices[0]) if prices else 0.0,
            "volume": float(mkt.get("volume", 0)),
            "best_ask": float(mkt.get("bestAsk", 0) or 0),
            "best_bid": float(mkt.get("bestBid", 0) or 0),
            "clob_tokens": mkt.get("clobTokenIds", []),
        })
    
    logger.info("   %d active markets fetched", len(markets))
    return markets


# ═══════════════════════════════════════════════════════════════
# MARKET PARSING — Multi-variable support
# ═══════════════════════════════════════════════════════════════

def match_city(slug: str) -> Optional[str]:
    s = slug.lower()
    for city in FOCUS_CITIES:
        parts = city.split("-")
        if all(p in s for p in parts):
            return city
    return None


def parse_market(question: str) -> Optional[dict]:
    """Parse market question. Now supports: temp_max, temp_min, precipitation, between."""
    q = question.lower()
    
    # Detect variable type
    variable = "temp_max"
    if "lowest" in q or "minimum" in q or "min temp" in q:
        variable = "temp_min"
    elif "precipitation" in q or "rain" in q or "mm" in q:
        variable = "precip"
    
    # Detect unit
    unit = "°C"
    if "°f" in q or "fahrenheit" in q or re.search(r'\d+\s*f\b', q):
        unit = "°F"
    if variable == "precip":
        unit = "mm"
    
    # Extract numbers
    nums = re.findall(r'(\d+(?:\.\d+)?)\s*°?\s*[cfCF]?', q)
    if not nums:
        nums = re.findall(r'(\d+(?:\.\d+)?)\s*(?:degrees?|mm|°)?', q)
        nums = [n for n in nums if float(n) > 1]
        if variable == "precip":
            nums = [n for n in nums if 10 <= float(n) <= 2000]
        else:
            nums = [n for n in nums if 5 <= float(n) <= 130]
    if not nums:
        return None
    
    nums_f = [float(n) for n in nums]
    
    # Detect direction
    if "below" in q or "or below" in q or "less than" in q or "under" in q:
        direction = "below"
        threshold = max(nums_f)
    elif "between" in q and len(nums_f) >= 2:
        direction = "between"
        lo, hi = sorted(nums_f[:2])
        threshold = hi
    elif "above" in q or "or above" in q or "over" in q or "more than" in q or "exceed" in q:
        direction = "above"
        threshold = min(nums_f) if variable == "precip" else nums_f[0]
    else:
        direction = "above"
        threshold = nums_f[0] if nums_f else 0
    
    return {
        "threshold": threshold,
        "unit": unit,
        "direction": direction,
        "variable": variable,
    }


def match_forecast_variable(fc: dict, parsed_variable: str) -> bool:
    """Check if forecast data matches the market variable type."""
    fc_var = fc.get("variable", "temp_max")
    if parsed_variable in ("temp_max", "temp_min") and fc_var in ("temp_max", "temp_min"):
        # We can compute min from max (rough approximation) or use separate data
        return fc_var == parsed_variable or fc_var == "temp_max"
    return fc_var == parsed_variable


# ═══════════════════════════════════════════════════════════════
# SIGNAL COMPUTATION
# ═══════════════════════════════════════════════════════════════

def compute_signal(forecast: dict, market: dict, pm: PortfolioManager) -> Optional[dict]:
    """Compute edge, confidence, position size for a market-forecast pair."""
    parsed = parse_market(market.get("question", ""))
    if not parsed:
        return None
    
    threshold = parsed["threshold"]
    unit = parsed["unit"]
    direction = parsed["direction"]
    variable = parsed["variable"]
    market_prob = market.get("price_yes", 0.5)
    
    # Match forecast variable to market variable
    fc_value = forecast.get("value")
    if fc_value is None:
        return None
    
    sigma = forecast.get("sigma", 1.5)
    n_members = forecast.get("n_members", 1)
    
    # Unit conversion
    if unit == "°F":
        fc_compare = fc_value * 9/5 + 32
        sigma = sigma * 9/5
    else:
        fc_compare = fc_value
    
    # ── For temp_min: approximate from temp_max if we only have max ──
    # This is a heuristic — ideally we'd fetch min directly
    if variable == "temp_min" and forecast.get("variable") == "temp_max":
        fc_compare = fc_compare - 8  # typical diurnal range ~8°C
        sigma = sigma * 1.2  # more uncertainty for min estimates
    
    # Compute model probability
    pe = ProbabilityEngine(sigma)
    
    if direction == "below":
        model_prob = pe.prob_below(fc_compare, threshold)
    elif direction == "between":
        nums = re.findall(r'(\d+(?:\.\d+)?)', market.get("question", "").lower())
        lo_val = min([float(n) for n in nums[:2]]) if len(nums) >= 2 else threshold - 5
        hi_val = threshold
        model_prob = pe.prob_between(fc_compare, lo_val, hi_val)
    elif direction == "above" and variable == "precip":
        model_prob = pe.prob_above(fc_compare, threshold)
    else:  # "above" for temperature
        model_prob = pe.prob_above(fc_compare, threshold)
    
    # Edge
    edge = model_prob - market_prob
    
    # Sanity: cap extreme edges (thin markets at $0.01 or $0.99)
    edge = max(-MAX_EDGE, min(MAX_EDGE, edge))
    
    # Skip if market is too thin (both extremes)
    if market_prob < MIN_MARKET_PROB or market_prob > (1 - MIN_MARKET_PROB):
        # If model still has strong signal, keep it but flag
        if abs(edge) < 0.15:
            return None  # thin market, low edge → skip
        # Otherwise allow but the edge is capped above
    
    # Confidence
    ce = ConfidenceEngine()
    confidence = ce.compute(sigma, n_members, fc_compare, threshold)
    
    # Tier and position size
    tier = pm.signal_tier(edge, confidence)
    position_size = pm.size_position(edge, market_prob, confidence)
    
    # Signal decision
    if edge >= MIN_EDGE and confidence >= MIN_CONFIDENCE:
        signal = "BUY YES"
    elif edge <= -MIN_EDGE and confidence >= MIN_CONFIDENCE:
        signal = "BUY NO"
    else:
        signal = "PASS"
        position_size = 0
    
    return {
        "slug": market["slug"],
        "city": forecast["city"],
        "date": forecast["date"],
        "question": market.get("question", "")[:120],
        "threshold": threshold,
        "unit": unit,
        "direction": direction,
        "variable": variable,
        "market_prob": round(market_prob, 4),
        "model_prob": round(model_prob, 4),
        "edge": round(edge, 4),
        "confidence": confidence,
        "signal": signal,
        "tier": tier,
        "fc_value": round(fc_value, 1),
        "fc_compare": round(fc_compare, 1),
        "position_size": position_size,
        "volume": market.get("volume", 0),
        "best_ask": market.get("best_ask", 0),
        "best_bid": market.get("best_bid", 0),
        "clob_tokens": market.get("clob_tokens", []),
    }


# ═══════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    slug TEXT NOT NULL,
    city TEXT,
    date TEXT,
    question TEXT,
    threshold REAL,
    unit TEXT,
    direction TEXT,
    variable TEXT,
    market_prob REAL,
    model_prob REAL,
    edge REAL,
    confidence INTEGER,
    signal TEXT,
    tier TEXT,
    fc_value REAL,
    position_size REAL,
    volume REAL
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT NOT NULL,
    slug TEXT NOT NULL,
    city TEXT,
    question TEXT,
    signal TEXT,
    tier TEXT,
    edge REAL,
    confidence INTEGER,
    entry_price REAL,
    shares INTEGER,
    cost REAL,
    capital_at_entry REAL,
    status TEXT DEFAULT 'open',
    resolved_at TEXT,
    resolved_pnl REAL,
    outcome TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_slug ON signals(slug);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initial_capital REAL NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);
"""


class AlphaDB:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
    
    def init_account(self, capital: float):
        existing = self.conn.execute("SELECT id FROM account WHERE id=1").fetchone()
        if not existing:
            self.conn.execute("INSERT INTO account (id, initial_capital) VALUES (1, ?)", (capital,))
        else:
            self.conn.execute("UPDATE account SET initial_capital=?, updated_at=datetime('now') WHERE id=1", (capital,))
        self.conn.commit()
    
    def get_balance(self) -> dict:
        acc = self.conn.execute("SELECT initial_capital FROM account WHERE id=1").fetchone()
        initial = acc[0] if acc else 0
        deployed = self.conn.execute(
            "SELECT COALESCE(SUM(cost), 0) FROM positions WHERE status='open'"
        ).fetchone()[0]
        resolved = self.conn.execute(
            "SELECT COALESCE(SUM(resolved_pnl), 0) FROM positions WHERE status='resolved'"
        ).fetchone()[0]
        return {
            "initial_capital": round(initial, 2),
            "deployed": round(deployed, 2),
            "realized_pnl": round(resolved, 2),
            "available": round(initial - deployed + resolved, 2),
        }
    
    def insert_signals(self, signals: List[dict]):
        for s in signals:
            self.conn.execute("""
                INSERT INTO signals (ts, slug, city, date, question, threshold, unit,
                    direction, variable, market_prob, model_prob, edge, confidence, signal,
                    tier, fc_value, position_size, volume)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                datetime.now(HKT).isoformat(), s["slug"], s["city"], s["date"],
                s["question"], s["threshold"], s["unit"], s["direction"],
                s.get("variable", "temp_max"),
                s["market_prob"], s["model_prob"], s["edge"], s["confidence"],
                s["signal"], s["tier"], s["fc_value"], s["position_size"],
                s.get("volume", 0)
            ))
        self.conn.commit()
    
    def open_positions(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC"
        ).fetchall()
        cols = [d[0] for d in self.conn.execute("PRAGMA table_info(positions)")]
        return [dict(zip(cols, r)) for r in rows]
    
    def open_position(self, slug: str, city: str, question: str, signal: str,
                      tier: str, edge: float, confidence: int,
                      entry_price: float, shares: int, cost: float, capital: float):
        existing = self.conn.execute(
            "SELECT id FROM positions WHERE slug=? AND status='open'", (slug,)
        ).fetchone()
        if existing:
            return
        self.conn.execute("""
            INSERT INTO positions (opened_at, slug, city, question, signal, tier,
                edge, confidence, entry_price, shares, cost, capital_at_entry)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now(HKT).isoformat(), slug, city, question, signal, tier,
            edge, confidence, entry_price, shares, cost, capital
        ))
        self.conn.commit()
    
    def get_stats(self) -> dict:
        total_signals = self.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        total_positions = self.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        open_pos = self.conn.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0]
        resolved = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(resolved_pnl),0) FROM positions WHERE status='resolved'"
        ).fetchone()
        wins = self.conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status='resolved' AND resolved_pnl > 0"
        ).fetchone()[0]
        losses = self.conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status='resolved' AND resolved_pnl < 0"
        ).fetchone()[0]
        return {
            "total_signals": total_signals,
            "total_positions": total_positions,
            "open": open_pos,
            "resolved": resolved[0] or 0,
            "realized_pnl": round(resolved[1] or 0, 2),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / max(resolved[0], 1) * 100, 1) if resolved[0] else 0,
        }


# ═══════════════════════════════════════════════════════════════
# MAIN ENGINE
# ═══════════════════════════════════════════════════════════════

class WeatherAlpha:
    def __init__(self, live: bool = False, capital: float = DEFAULT_CAPITAL):
        self.live = live
        self.capital = capital
        self.pm = PortfolioManager(capital=capital)
        self.db = AlphaDB() if live else None
        if self.db:
            self.db.init_account(capital)
    
    def run(self) -> List[dict]:
        t0 = time.time()
        logger.info("=" * 70)
        logger.info("🌦 Weather Alpha v4 — Scan @ %s", datetime.now(HKT).strftime("%H:%M:%S"))
        logger.info("=" * 70)
        
        # 1. Fetch forecasts
        logger.info("📡 Fetching forecasts for %d cities...", len(FOCUS_CITIES))
        forecasts = fetch_all_forecasts()
        total_fc = sum(len(v) for v in forecasts.values())
        logger.info("   %d forecasts across %d days (max, min, precip)", total_fc, len(forecasts))
        
        if not forecasts:
            logger.warning("   ⚠️ No forecast data")
            return []
        
        # 2. Discover markets
        markets = discover_markets()
        if not markets:
            logger.warning("   ⚠️ No markets discovered")
            return []
        
        # 3. Build forecast lookup
        fc_lookup = {}
        for date_str, fc_list in forecasts.items():
            for fc in fc_list:
                key = (fc["city"], date_str, fc.get("variable", "temp_max"))
                fc_lookup[key] = fc
        
        # 4. Match & compute signals — MULTI-BIN: match ALL bins per city
        signals = []
        for mkt in markets:
            city = match_city(mkt.get("slug", ""))
            if not city:
                continue
            
            parsed = parse_market(mkt.get("question", ""))
            if not parsed:
                continue
            
            var = parsed["variable"]
            
            for date_str in forecasts:
                fc_key = (city, date_str, var)
                fc = fc_lookup.get(fc_key)
                
                # Fallback: use temp_max forecast for temp_min markets
                if not fc and var == "temp_min":
                    fc = fc_lookup.get((city, date_str, "temp_max"))
                
                if not fc:
                    continue
                
                sig = compute_signal(fc, mkt, self.pm)
                if sig and sig["signal"] != "PASS":
                    signals.append(sig)
        
        # 5. Deduplicate by (city, date, threshold, direction, variable)
        # Multiple slugs can match the same market — keep only the best edge per key
        best_by_key = {}
        for s in signals:
            key = (s["city"], s["date"], s["threshold"], s["direction"], s["variable"])
            if key not in best_by_key or abs(s["edge"]) > abs(best_by_key[key]["edge"]):
                best_by_key[key] = s
        signals = sorted(best_by_key.values(), key=lambda s: -abs(s["edge"]))
        
        # 6. Persist
        if self.live and signals:
            self.db.insert_signals(signals)
            self._auto_trade(signals)
        
        elapsed = time.time() - t0
        logger.info("✅ Scan complete in %.1fs — %d signals", elapsed, len(signals))
        return signals
    
    def _auto_trade(self, signals: List[dict]):
        """WeatherHK-style auto-trade: accept tier A+B, not just A."""
        bal = self.db.get_balance()
        available = bal["available"]
        open_count = len(self.db.open_positions())
        
        if open_count >= MAX_TOTAL_POSITIONS:
            logger.info("   🛑 Position cap reached (%d/%d)", open_count, MAX_TOTAL_POSITIONS)
            return
        
        # Use actual available balance
        self.pm.capital = available
        
        # Track positions opened per city to prevent over-concentration
        city_count = defaultdict(int)
        for p in self.db.open_positions():
            city_count[p.get("city", "")] += 1
        
        MAX_PER_CITY = 5  # max 5 positions per city
        
        for s in signals:
            if s["tier"] != "A":  # was A-only, now A+B
                continue
            if s["signal"] != "BUY NO":
                continue
            if s["position_size"] <= 0:
                continue
            
            # City concentration check
            if city_count.get(s["city"], 0) >= MAX_PER_CITY:
                continue
            
            entry_price = s["best_ask"] if s["signal"] == "BUY YES" else (1.0 - s["best_bid"])
            if entry_price <= 0.01 or entry_price >= 0.99 or (s["signal"] == "BUY NO" and entry_price > 0.65):
                continue
            
            # Scale position: use the Kelly-calculated size
            size_usdc = s["position_size"]
            # But cap at available balance
            size_usdc = min(size_usdc, available - MIN_AVAILABLE, 2.0)
            if size_usdc < 1:  # minimum $1 per trade
                continue
            
            shares = int(size_usdc / entry_price)
            cost = shares * entry_price
            
            if cost > available - MIN_AVAILABLE:
                continue
            if open_count >= MAX_TOTAL_POSITIONS:
                break
            
            # Dedup: skip if already have open position for this slug
            existing = self.db.conn.execute(
                "SELECT id FROM positions WHERE slug=? AND status='open'", (s["slug"],)
            ).fetchone()
            if existing:
                continue

            # Place real CLOB order via Node.js bridge
            tokens = s.get("clob_tokens", [])
            if isinstance(tokens, str):
                try: tokens = json.loads(tokens)
                except: tokens = []
            token_id = tokens[1] if len(tokens) > 1 else ""
            clob_result = place_clob_order(token_id, entry_price, shares, "BUY") if token_id else {"success": False, "error": "no token"}
            ok = clob_result.get("success")
            detail = clob_result.get("order_id", "")[:12] if ok else clob_result.get("error", "")[:30]
            logger.info("   [%s] CLOB %s", "OK" if ok else "FAIL", detail)

            self.db.open_position(
                slug=s["slug"], city=s["city"], question=s["question"],
                signal=s["signal"], tier=s["tier"], edge=s["edge"],
                confidence=s["confidence"], entry_price=entry_price,
                shares=shares, cost=cost, capital=available
            )
            available -= cost
            open_count += 1
            city_count[s["city"]] += 1
            self.pm.capital = available

            logger.info("   📊 OPEN %s %s | %s | %dsh @ $%.4f = $%.2f | bal=$%.2f",
                        s["tier"], s["signal"], s["city"], shares, entry_price, cost, available)
    
    def report(self, signals: List[dict]):
        if not signals:
            logger.info("📊 No qualifying signals found.")
            return
        
        tiers = defaultdict(int)
        cities = defaultdict(int)
        for s in signals:
            tiers[s["tier"]] += 1
            cities[s["city"]] += 1
        
        logger.info("\n" + "═" * 85)
        logger.info("🌦 WEATHER ALPHA v4 SIGNALS — %d signals across %d cities",
                     len(signals), len(cities))
        logger.info("   Tiers: %s", ", ".join(f"{k}:{v}" for k, v in sorted(tiers.items())))
        logger.info("   Cities: %s", ", ".join(f"{c}({n})" for c, n in sorted(cities.items(), key=lambda x: -x[1])[:10]))
        logger.info("═" * 85)
        logger.info("%-2s %-7s %-4s %-14s %-6s %-5s %-5s %-6s %-6s %-7s %-3s %-7s %-10s" % (
            "", "Signal", "Tier", "City", "Dir", "Thr", "Fc", "Mkt", "Mdl", "Edge", "Cnf", "Size", "Var"))
        logger.info("─" * 85)
        
        for s in signals[:40]:
            emoji = {"BUY YES": "🟢", "BUY NO": "🔴"}.get(s["signal"], "⚪")
            fc_display = s.get("fc_compare", s["fc_value"])
            var_short = {"temp_max": "Tmax", "temp_min": "Tmin", "precip": "Rain"}.get(s.get("variable", ""), "")
            logger.info(
                "%s %-7s %-4s %-14s %-6s %4.0f%s %4.0f%s %5.1f%% %5.1f%% %+6.1f%% %2d%% $%6.2f %-6s" % (
                    emoji, s["signal"], s["tier"], s["city"][:14],
                    s["direction"], s["threshold"], s["unit"],
                    fc_display, s["unit"],
                    s["market_prob"] * 100, s["model_prob"] * 100,
                    s["edge"] * 100, s["confidence"],
                    s["position_size"], var_short
                ))
        
        logger.info("═" * 85 + "\n")


def main():
    p = argparse.ArgumentParser(description="Weather Alpha v4 — WeatherHK-Inspired")
    p.add_argument("--live", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    p.add_argument("--interval", type=int, default=POLL_INTERVAL)
    args = p.parse_args()
    
    alpha = WeatherAlpha(live=args.live or args.once, capital=args.capital)
    signals = alpha.run()
    alpha.report(signals)
    
    if args.once or not args.live:
        return
    
    running = True
    def stop(*_):
        nonlocal running; running = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    
    logger.info("🔄 Live mode — scanning every %ds", args.interval)
    while running:
        try:
            time.sleep(args.interval)
            signals = alpha.run()
            alpha.report(signals)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error("❌ Scan error: %s", e)
            time.sleep(10)
    logger.info("👋 Weather Alpha v4 stopped.")


if __name__ == "__main__":
    main()
