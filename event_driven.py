"""Event-Driven Engine - Predictable liquidity events"""

import json
import time
import logging
import numpy as np
from datetime import datetime, timezone, timedelta
from config import *

logger = logging.getLogger(__name__)


class EventCalendar:
    """Track scheduled events that affect BTC liquidity."""
    
    # Major economic events (simplified calendar)
    EVENTS = {
        # US Market Hours (UTC)
        "us_market_open": {"hour": 14, "minute": 30, "impact": "high", "description": "US Stock Market Open"},
        "us_market_close": {"hour": 21, "minute": 0, "impact": "high", "description": "US Stock Market Close"},
        
        # Economic Data Releases (UTC)
        "cpi": {"hour": 13, "minute": 30, "impact": "high", "description": "CPI Data Release"},
        "nonfarm": {"hour": 13, "minute": 30, "impact": "high", "description": "Non-Farm Payrolls"},
        "fed_rate": {"hour": 19, "minute": 0, "impact": "high", "description": "Fed Rate Decision"},
        "fed_speech": {"hour": 15, "minute": 0, "impact": "medium", "description": "Fed Chair Speech"},
        
        # Crypto-specific
        "btc_futures_expiry": {"hour": 8, "minute": 0, "impact": "medium", "description": "BTC Futures Expiry (Friday)"},
        "eth_merge": {"hour": 2, "minute": 0, "impact": "high", "description": "ETH Merge/Upgrade"},
    }
    
    def get_upcoming_events(self, hours_ahead=6) -> list:
        """Get events in the next N hours."""
        now = datetime.now(timezone.utc)
        upcoming = []
        
        for event_name, event_info in self.EVENTS.items():
            event_time = now.replace(hour=event_info["hour"], minute=event_info["minute"], second=0, microsecond=0)
            
            if event_time < now:
                event_time += timedelta(days=1)
            
            if event_time <= now + timedelta(hours=hours_ahead):
                upcoming.append({
                    "name": event_name,
                    "time": event_time.isoformat(),
                    "impact": event_info["impact"],
                    "description": event_info["description"],
                    "minutes_until": int((event_time - now).total_seconds() / 60),
                })
        
        return sorted(upcoming, key=lambda x: x["minutes_until"])
    
    def get_current_regime(self) -> dict:
        """Get current market regime based on time."""
        now = datetime.now(timezone.utc)
        hour = now.hour
        minute = now.minute
        weekday = now.weekday()  # 0=Monday, 4=Friday
        
        # Determine regime
        if 14 <= hour < 21:  # US market hours
            regime = "us_session"
            volatility = "high"
        elif 0 <= hour < 6:  # Asian night
            regime = "asian_night"
            volatility = "low"
        elif 6 <= hour < 14:  # Asian day
            regime = "asian_session"
            volatility = "medium"
        else:  # European
            regime = "european_session"
            volatility = "medium"
        
        # Friday effects
        if weekday == 4 and hour >= 8:
            regime = "friday_expiry"
            volatility = "very_high"
        
        return {
            "regime": regime,
            "volatility": volatility,
            "hour_utc": hour,
            "minute_utc": minute,
            "weekday": weekday,
        }


class EventDrivenEngine:
    """Event-driven prediction engine."""
    
    def __init__(self):
        self.calendar = EventCalendar()
        
    def analyze(self) -> dict:
        """Analyze event-driven signals."""
        signals = []
        score = 0.0
        
        # 1. Get current regime
        regime = self.calendar.get_current_regime()
        
        if regime["volatility"] == "high":
            signals.append(f"High volatility regime: {regime['regime']}")
        elif regime["volatility"] == "very_high":
            signals.append(f"⚠️ Very high volatility: {regime['regime']} (Friday expiry)")
            score += 0.10  # Expect larger moves
        else:
            signals.append(f"Low volatility regime: {regime['regime']}")
        
        # 2. Check upcoming events
        upcoming = self.calendar.get_upcoming_events(hours_ahead=2)
        
        if upcoming:
            for event in upcoming:
                if event["impact"] == "high":
                    if event["minutes_until"] < 30:
                        score += 0.15
                        signals.append(f"⚠️ HIGH IMPACT EVENT in {event['minutes_until']}min: {event['description']}")
                    elif event["minutes_until"] < 60:
                        score += 0.08
                        signals.append(f"Event in {event['minutes_until']}min: {event['description']}")
                    else:
                        signals.append(f"Event in {event['minutes_until']}min: {event['description']}")
        
        # 3. Time-based patterns
        hour = regime["hour_utc"]
        
        # 14:30 UTC = US market open → often sees directional move
        if 14 <= hour < 15:
            signals.append("US market open → potential directional move")
            score += 0.05
        
        # 21:00 UTC = US market close → often sees reversal
        if 20 <= hour < 22:
            signals.append("US market close → potential reversal")
            score += 0.05
        
        # 08:00 UTC = Asian session start → often sees range
        if 7 <= hour < 9:
            signals.append("Asian session start → often range-bound")
        
        confidence = min(1.0, 0.5 + score)
        
        return {
            "type": "event_driven",
            "confidence": float(confidence),
            "score": float(score),
            "signals": signals,
            "regime": regime["regime"],
            "volatility": regime["volatility"],
            "upcoming_events": upcoming,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


class NewsAnalyzer:
    """Analyze news impact (placeholder for future news API integration)."""
    
    def __init__(self):
        self.last_news_check = 0
        self.news_cache = []
        
    def check_news(self) -> dict:
        """Check for breaking news (placeholder)."""
        # In production, this would call a news API
        # For now, return neutral
        
        return {
            "type": "news",
            "sentiment": "neutral",
            "signals": ["News analysis not yet implemented"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
