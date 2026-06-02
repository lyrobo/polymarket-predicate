"""
Order Flow Analyzer - Real-time order book, CVD, funding, OI via WebSocket
===========================================================================

Uses OKX WebSocket streams for:
  - Order book depth (books5, 100ms updates)
  - Real-time trades (tick-by-tick CVD)
  - Funding rate & mark price (perpetual swap)

Also supports HTTP fallback via Binance/OKX REST APIs.
"""

import json
import time
import logging
import numpy as np
import urllib.request
import ssl
from datetime import datetime, timezone, timedelta
from collections import deque
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)
ctx = ssl._create_unverified_context()


class OrderBookAnalyzer:
    """Analyze order book imbalance and liquidity from WebSocket or REST."""
    
    def __init__(self, depth=20):
        self.depth = depth
        self.history = deque(maxlen=100)  # Last 100 snapshots
        self._ws_client = None  # Set externally if using WebSocket
        
    def set_ws_client(self, client):
        """Attach WebSocket client for real-time data."""
        self._ws_client = client
    
    def get_current_snapshot(self) -> Optional[Dict]:
        """Get latest order book snapshot from WebSocket or REST."""
        # Try WebSocket first
        if self._ws_client:
            ob = self._ws_client.get_order_book()
            if ob.update_count > 0:
                snap = ob.get_snapshot()
                bids = snap['bids']
                asks = snap['asks']
                if bids and asks:
                    return self._process_snapshot(bids, asks)
        
        # Fallback to REST
        return self._fetch_rest()
    
    def _process_snapshot(self, bids, asks) -> Dict:
        """Process bid/ask arrays into analysis result."""
        bid_volume = sum(q for _, q in bids)
        ask_volume = sum(q for _, q in asks)
        
        bid_weighted = sum(p * q for p, q in bids)
        ask_weighted = sum(p * q for p, q in asks)
        
        imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume) if (bid_volume + ask_volume) > 0 else 0
        weighted_imbalance = (bid_weighted - ask_weighted) / (bid_weighted + ask_weighted) if (bid_weighted + ask_weighted) > 0 else 0
        
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        spread_bps = (best_ask - best_bid) / best_bid * 10000
        mid_price = (best_bid + best_ask) / 2
        
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bid_volume": float(bid_volume),
            "ask_volume": float(ask_volume),
            "imbalance": float(imbalance),
            "weighted_imbalance": float(weighted_imbalance),
            "spread_bps": float(spread_bps),
            "mid_price": float(mid_price),
            "bid_depth": float(bid_volume / mid_price) if mid_price > 0 else 0,
            "ask_depth": float(ask_volume / mid_price) if mid_price > 0 else 0,
        }
        
        self.history.append(result)
        return result
    
    def _fetch_rest(self) -> Optional[Dict]:
        """Fetch order book from OKX REST API."""
        url = "https://www.okx.com/api/v5/market/books?instId=BTC-USDT&sz=20"
        req = urllib.request.Request(url, headers={
            "User-Agent": "BTC-Predictor/1.0",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                data = json.loads(resp.read().decode())
                if data.get('code') != '0':
                    return None
                
                d = data['data'][0]
                bids = [(float(r[0]), float(r[1])) for r in d['bids'][:self.depth]]
                asks = [(float(r[0]), float(r[1])) for r in d['asks'][:self.depth]]
                return self._process_snapshot(bids, asks)
        except Exception as e:
            logger.error(f"Order book REST fetch failed: {e}")
            return None
    
    def get_trend(self, window=10):
        """Get imbalance trend over last N snapshots."""
        if len(self.history) < window:
            return None
        
        recent = list(self.history)[-window:]
        imbalances = [h["imbalance"] for h in recent]
        
        x = np.arange(len(imbalances))
        if np.std(x) == 0 or np.std(imbalances) == 0:
            return {"slope": 0, "direction": "neutral"}
        
        slope = np.polyfit(x, imbalances, 1)[0]
        
        return {
            "slope": float(slope),
            "direction": "increasing_bids" if slope > 0.01 else "increasing_asks" if slope < -0.01 else "neutral",
            "current_imbalance": imbalances[-1],
            "avg_imbalance": float(np.mean(imbalances)),
        }


class CVDEngine:
    """Cumulative Volume Delta from WebSocket trades or REST."""
    
    def __init__(self, window=100):
        self.window = window
        self.cvd = deque(maxlen=window)
        self._ws_client = None
    
    def set_ws_client(self, client):
        """Attach WebSocket client for real-time trade stream."""
        self._ws_client = client
    
    def get_current_cvd(self) -> Optional[Dict]:
        """Get CVD from WebSocket or REST."""
        if self._ws_client:
            ts = self._ws_client.get_trade_stream()
            # Only use if we have enough data
            if ts.update_count > 0 if hasattr(ts, 'update_count') else True:
                cvd = ts.get_cvd()
                ratio = ts.get_buy_sell_ratio()
                trades = ts.get_recent_trades(300)  # Last 5 min
                
                buys = sum(q for _, _, q, m in trades if not m)
                sells = sum(q for _, _, q, m in trades if m)
                delta = buys - sells
                
                result = {
                    "buys": float(buys),
                    "sells": float(sells),
                    "delta": float(delta),
                    "cumulative_delta": float(cvd),
                    "buy_ratio": max(0.1, min(0.9, ratio / (1 + ratio))) if ratio != float('inf') else 0.5,
                    "trade_count": len(trades),
                }
                self.cvd.append(result)
                return result
        
        # Fallback to REST
        return self._fetch_rest()
    
    def _fetch_rest(self) -> Optional[Dict]:
        """Fetch recent trades from OKX REST, fallback to Binance."""
        buys = 0
        sells = 0
        
        # Try OKX first
        url = "https://www.okx.com/api/v5/market/trades?instId=BTC-USDT&limit=100"
        req = urllib.request.Request(url, headers={"User-Agent": "BTC-Predictor/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                data = json.loads(resp.read().decode())
                if data.get('code') == '0':
                    for trade in data['data']:
                        vol = float(trade['sz'])
                        if trade['side'] == 'buy':
                            buys += vol
                        else:
                            sells += vol
        except Exception:
            # Fallback to Binance
            url_binance = "https://api.binance.com/api/v3/trades?symbol=BTCUSDT&limit=100"
            req_binance = urllib.request.Request(url_binance, headers={"User-Agent": "BTC-Predictor/1.0"})
            try:
                with urllib.request.urlopen(req_binance, timeout=10, context=ctx) as resp:
                    data = json.loads(resp.read().decode())
                    for trade in data:
                        vol = float(trade['qty'])
                        if not trade.get('isBuyerMaker', False):
                            buys += vol
                        else:
                            sells += vol
            except Exception as e:
                logger.debug(f"CVD REST fetch failed (OKX+Binance): {e}")
                return None
        
        if buys + sells == 0:
            return None
            
        delta = buys - sells
        result = {
            "buys": float(buys),
            "sells": float(sells),
            "delta": float(delta),
            "buy_ratio": max(0.1, min(0.9, buys / (buys + sells))),
        }
        self.cvd.append(result)
        return result
    def get_divergence(self):
        """Check for CVD-price divergence."""
        if len(self.cvd) < 20:
            return None
        
        recent = list(self.cvd)[-20:]
        deltas = [d['delta'] for d in recent]
        avg_delta = np.mean(deltas)
        
        return {
            "avg_delta_20": float(avg_delta),
            "signal": "bullish_divergence" if avg_delta > 50 else 
                     "bearish_divergence" if avg_delta < -50 else "neutral",
        }


class FundingAnalyzer:
    """Funding rate and open interest analysis."""
    
    def __init__(self):
        self._ws_client = None
        self.history = deque(maxlen=100)
    
    def set_ws_client(self, client):
        self._ws_client = client
    
    def get_current(self) -> Dict:
        """Get funding rate and mark price."""
        funding = 0.0
        mark_price = 0.0
        oi = 0.0
        
        if self._ws_client:
            funding = self._ws_client.funding_rate
            mark_price = self._ws_client.mark_price
        
        # Get OI from REST if needed
        if oi == 0:
            oi = self._fetch_open_interest()
        
        result = {
            "funding_rate": funding,
            "mark_price": mark_price,
            "open_interest": oi,
            "pressure": self._classify_pressure(funding, oi),
        }
        self.history.append(result)
        return result
    
    def _fetch_open_interest(self) -> float:
        """Fetch OI from OKX REST."""
        url = "https://www.okx.com/api/v5/account/account-position?instId=BTC-USDT-SWAP"
        # OKX doesn't have a public OI endpoint for swaps easily
        # Use Binance as fallback
        url_binance = "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"
        req = urllib.request.Request(url_binance, headers={"User-Agent": "BTC-Predictor/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                data = json.loads(resp.read().decode())
                return float(data.get('openInterest', 0))
        except:
            return 0
    
    def _classify_pressure(self, funding, oi):
        """Classify liquidation pressure."""
        if funding > 0.0001 and oi > 20e9:
            return "long_squeeze_risk"
        elif funding < -0.0001 and oi > 20e9:
            return "short_squeeze_risk"
        elif oi > 25e9:
            return "high_leverage"
        else:
            return "normal"


class OrderFlowEngine:
    """Combined order flow analysis engine with WebSocket support."""
    
    def __init__(self):
        self.ob = OrderBookAnalyzer()
        self.cvd = CVDEngine()
        self.funding = FundingAnalyzer()
        self._ws_attached = False
    
    def attach_websocket(self, ws_client):
        """Attach WebSocket client to all sub-engines."""
        self.ob.set_ws_client(ws_client)
        self.cvd.set_ws_client(ws_client)
        self.funding.set_ws_client(ws_client)
        self._ws_attached = True
        logger.info("WebSocket attached to OrderFlowEngine")
    
    def analyze(self) -> Dict:
        """Run full order flow analysis."""
        ob_data = self.ob.get_current_snapshot()
        cvd_data = self.cvd.get_current_cvd()
        funding_data = self.funding.get_current()
        ob_trend = self.ob.get_trend()
        cvd_div = self.cvd.get_divergence()
        
        # Generate signals
        signals = []
        score = 0.0
        
        # --- Order Book Imbalance ---
        if ob_data:
            imb = ob_data['imbalance']
            if imb > 0.1:
                score += 0.08
                signals.append(f"Order book: bid-heavy ({imb:.2%})")
            elif imb < -0.1:
                score -= 0.08
                signals.append(f"Order book: ask-heavy ({imb:.2%})")
            
            if ob_data['spread_bps'] < 2:
                signals.append(f"Tight spread ({ob_data['spread_bps']:.1f} bps) → imminent move")
        
        # --- Order Book Trend ---
        if ob_trend:
            if ob_trend['direction'] == 'increasing_bids':
                score += 0.05
                signals.append("Order book: bids increasing")
            elif ob_trend['direction'] == 'increasing_asks':
                score -= 0.05
                signals.append("Order book: asks increasing")
        
        # --- CVD ---
        if cvd_data:
            if cvd_data['buy_ratio'] > 0.55:
                score += 0.06
                signals.append(f"CVD: aggressive buying ({cvd_data['buy_ratio']:.1%})")
            elif cvd_data['buy_ratio'] < 0.45:
                score -= 0.06
                signals.append(f"CVD: aggressive selling ({cvd_data['buy_ratio']:.1%})")
        
        # --- Funding & OI ---
        if funding_data:
            if funding_data['pressure'] == 'long_squeeze_risk':
                score -= 0.12
                signals.append("⚠️ Long squeeze risk (high funding + high OI)")
            elif funding_data['pressure'] == 'short_squeeze_risk':
                score += 0.12
                signals.append("⚠️ Short squeeze risk (negative funding + high OI)")
            
            fr = funding_data['funding_rate']
            if fr > 0.0001:
                signals.append(f"Funding: positive ({fr:.4%}) → longs pay")
            elif fr < -0.0001:
                signals.append(f"Funding: negative ({fr:.4%}) → shorts pay")
        
        confidence = max(0.0, min(1.0, 0.5 + score / 2))
        # Neutral zone: no random direction — return HOLD/NEUTRAL
        if abs(score) < 0.05:
            direction = 0
        else:
            direction = 1 if confidence > 0.5 else -1
        
        return {
            "direction": direction,
            "confidence": float(confidence),
            "score": float(score),
            "signals": signals,
            "order_book": ob_data,
            "cvd": cvd_data,
            "funding": funding_data,
            "data_source": "websocket" if self._ws_attached else "rest",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    
    engine = OrderFlowEngine()
    
    print("Order Flow Analysis (REST mode)")
    print("=" * 60)
    
    for i in range(3):
        result = engine.analyze()
        print(f"\n[{result['timestamp']}]")
        print(f"  Source: {result['data_source']}")
        print(f"  Direction: {'UP' if result['direction']==1 else 'DN'} ({result['confidence']:.1%})")
        print(f"  Signals:")
        for s in result['signals']:
            print(f"    - {s}")
        
        if result['order_book']:
            ob = result['order_book']
            print(f"  Order Book: imbalance={ob['imbalance']:.2%}, spread={ob['spread_bps']:.1f} bps")
        
        if result['cvd']:
            cvd = result['cvd']
            print(f"  CVD: buy_ratio={cvd['buy_ratio']:.1%}, delta={cvd['delta']:.4f}")
        
        if result['funding']:
            f = result['funding']
            print(f"  Funding: rate={f['funding_rate']:.6f}, pressure={f['pressure']}")
        
        time.sleep(2)
