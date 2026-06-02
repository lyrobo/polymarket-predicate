"""
Real-time BTC 5-min Prediction Service with Polymarket Edge Detection
=====================================================================
Integrates WebSocket data + Unified Strategy Engine + Polymarket odds + SQLite logging

Usage:
    python3 realtime_service.py              # Run continuously (default)
    python3 realtime_service.py --once       # Single prediction
    python3 realtime_service.py --interval N # Custom interval (seconds)
"""

import json
import time
import logging
import sqlite3
import sys
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from threading import Thread

# Add project to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from websocket_collector import WebSocketClient
from unified_strategy import UnifiedStrategyEngine

# Ensure directories exist
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
DB_PATH = os.path.join(DATA_DIR, 'btc_predictor.db')

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'realtime.log')),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)

# Polymarket API
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
EDGE_THRESHOLD = 0.03  # 3% minimum edge


class PolymarketEdgeFinder:
    """Find BTC 5-minute markets on Polymarket and compute edge vs our prediction."""

    def __init__(self):
        self._cache = {}
        self._cache_time = 0
        self._cache_ttl = 30  # 30s cache

    def get_current_market(self) -> dict:
        """Get the current or next active BTC 5-min market with odds.
        
        Priority: next upcoming active+unclosed > current active+unclosed > any active.
        Skip closed markets entirely.
        """
        now = time.time()
        if self._cache and now - self._cache_time < self._cache_ttl:
            return self._cache.get("market")

        now_dt = datetime.now(timezone.utc)
        current_5min = now_dt.replace(second=0, microsecond=0)
        current_5min = current_5min.replace(minute=(current_5min.minute // 5) * 5)

        # Collect all candidate markets
        candidates = []

        # Search nearby windows: current, next 4, past 1
        for offset_min in range(-5, 25, 5):
            window_end = current_5min + timedelta(minutes=offset_min)
            ts = int(window_end.timestamp())
            slug = f"btc-updown-5m-{ts}"

            market = self._fetch_market(slug, window_end)
            if market:
                candidates.append((offset_min, market))

        # Filter: only active markets, skip closed AND past ones
        now_ts = time.time()
        active = [
            (off, m) for off, m in candidates
            if m.get('active') and not m.get('closed')
            and m.get('window_end_ts', 0) > now_ts  # window hasn't ended yet
        ]

        # Sort: prefer future windows (positive offset), then smallest offset
        active.sort(key=lambda x: (0 if x[0] >= 0 else 1, abs(x[0])))

        best_market = active[0][1] if active else None

        # If no active+unclosed, try any active
        if not best_market:
            any_active = [(off, m) for off, m in candidates if m.get('active')]
            any_active.sort(key=lambda x: x[0])
            best_market = any_active[0][1] if any_active else None

        self._cache = {"market": best_market, "time": now}
        return best_market

    def _fetch_market(self, slug: str, window_end: datetime) -> dict:
        """Fetch a single market by slug via events endpoint."""
        url = f"{POLYMARKET_GAMMA}/events?slug={urllib.parse.quote(slug)}"
        req = urllib.request.Request(url, headers={"User-Agent": "BTC-Predictor/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                if not data or not isinstance(data, list) or len(data) == 0:
                    return None

                event = data[0]
                markets = event.get("markets", [])
                if not markets or not isinstance(markets, list):
                    return None

                m = markets[0]
                prices_raw = json.loads(m.get("outcomePrices", "[]"))
                outcomes = json.loads(m.get("outcomes", "[]"))

                # outcomePrices[0] = "Up" price, [1] = "Down" price
                up_price = float(prices_raw[0]) if isinstance(prices_raw, list) and len(prices_raw) >= 2 else 0.5
                down_price = float(prices_raw[1]) if isinstance(prices_raw, list) and len(prices_raw) >= 2 else 0.5

                return {
                    "slug": slug,
                    "question": m.get("question", ""),
                    "outcomes": outcomes if isinstance(outcomes, list) else ["Up", "Down"],
                    "up_price": up_price,
                    "down_price": down_price,
                    "volume": float(m.get("volume", event.get("volume", 0))),
                    "liquidity": float(m.get("liquidity", event.get("liquidity", 0))),
                    "active": event.get("active", m.get("active", False)),
                    "closed": event.get("closed", m.get("closed", False)),
                    "condition_id": m.get("conditionId", ""),
                    "token_ids": json.loads(m.get("clobTokenIds", "[]")),
                    "end_date": event.get("endDate", m.get("endDate", "")),
                    "window_end": window_end.isoformat(),
                    "window_end_ts": window_end.timestamp(),
                }
        except Exception as e:
            logger.debug(f"Market fetch failed for {slug}: {e}")
            return None

    def compute_edge(self, prediction: dict, market: dict) -> dict:
        """Compare our prediction confidence vs Polymarket odds.

        BIDIRECTIONAL STRATEGY: Bet UP when predicting UP with positive edge,
        bet DOWN when predicting DN with positive edge.
        """
        pred_dir = prediction.get('direction', 1)
        our_conf = prediction.get('confidence', 0.5)

        up_price = market.get('up_price', 0.5)
        down_price = market.get('down_price', 0.5)

        # Bidirectional: bet on whatever direction we predict
        if pred_dir == 1:
            # Predict UP → bet UP
            our_prob = our_conf
            market_price = up_price
            bet_on = "Up"
        else:
            # Predict DN → bet DOWN
            our_prob = our_conf
            market_price = down_price
            bet_on = "Down"

        edge = our_prob - market_price
        our_prob_up = our_conf if pred_dir == 1 else (1.0 - our_conf)

        # Expected value per $1 bet
        ev_per_share = our_prob * 1.0 - market_price

        # Action recommendation
        if edge >= EDGE_THRESHOLD and our_conf >= 0.55:
            action = "BUY_UP" if bet_on == "Up" else "BUY_DN"
        elif abs(edge) >= EDGE_THRESHOLD:
            action = "WEAK_EDGE"
        else:
            action = "HOLD"

        return {
            "market_slug": market.get('slug', ''),
            "market_question": market.get('question', ''),
            "market_up_price": round(up_price, 4),
            "market_down_price": round(down_price, 4),
            "our_direction": "UP" if pred_dir == 1 else "DN",
            "our_confidence": round(our_conf, 4),
            "our_prob_for_bet": round(our_prob_up, 4),
            "market_price": round(market_price, 4),
            "edge": round(edge, 4),
            "edge_pct": f"{edge*100:.2f}%",
            "ev_per_share": round(ev_per_share, 4),
            "bet_on": bet_on,
            "action": action,
            "market_active": market.get('active', False),
            "market_closed": market.get('closed', False),
            "market_volume": market.get('volume', 0),
            "market_liquidity": market.get('liquidity', 0),
            "recommendation": self._format_recommendation(action, edge, our_conf, bet_on, market_price),
        }

    def _format_recommendation(self, action, edge, conf, bet_on, market_price):
        if action.startswith("BUY_"):
            return f"✅ EDGE FOUND! {action} at ${market_price:.3f} (our prob: {conf:.3f}, edge: {edge*100:.2f}%)"
        elif action == "WEAK_EDGE":
            return f"⚡ Small edge ({edge*100:.2f}%) — consider if risk tolerance allows"
        else:
            return f"⏸ No edge (market: ${market_price:.3f}, our: {conf:.3f}) — HOLD"


class RealtimePredictor:
    """Real-time prediction service with WebSocket + Polymarket edge detection."""

    def __init__(self, interval=30):
        self.interval = interval
        self.ws = WebSocketClient(symbol="BTC-USDT")
        self.strategy = UnifiedStrategyEngine()
        self.poly = PolymarketEdgeFinder()
        self.running = False
        self.cycle_count = 0
        self.last_prediction = None
        self.last_edge = None

        # Database
        self.db_path = DB_PATH
        self._init_db()

        # Attach WebSocket to strategy
        self.strategy.attach_websocket(self.ws)

    def _init_db(self):
        """Initialize SQLite database with edge columns."""
        conn = sqlite3.connect(self.db_path)

        # Create table if not exists
        conn.execute('''
            CREATE TABLE IF NOT EXISTS realtime_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                mid_price REAL,
                spread REAL,
                ob_imbalance REAL,
                cvd REAL,
                buy_sell_ratio REAL,
                funding_rate REAL,
                mark_price REAL,
                direction INTEGER,
                confidence REAL,
                score REAL,
                signals TEXT,
                data_source TEXT,
                cycle INTEGER
            )
        ''')

        # Create edge-specific table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS polymarket_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                market_slug TEXT,
                market_question TEXT,
                market_up_price REAL,
                market_down_price REAL,
                our_direction TEXT,
                our_confidence REAL,
                our_prob REAL,
                market_price REAL,
                edge REAL,
                edge_pct TEXT,
                ev_per_share REAL,
                bet_on TEXT,
                action TEXT,
                market_active INTEGER,
                market_closed INTEGER,
                market_volume REAL,
                market_liquidity REAL,
                recommendation TEXT,
                cycle INTEGER
            )
        ''')

        conn.commit()
        conn.close()
        logger.info(f"Database initialized: {self.db_path}")

    def _log_prediction(self, prediction: dict, ws_status: dict):
        """Log prediction to SQLite."""
        conn = sqlite3.connect(self.db_path)
        mid_price = ws_status.get('mid_price', 0)
        # Get edge data if available
        edge = getattr(self, 'last_edge', None)
        market_slug = edge.get('market_slug', '') if edge else ''
        market_question = edge.get('market_question', '') if edge else ''
        edge_val = edge.get('edge', 0) if edge else 0
        our_conf = edge.get('our_confidence', prediction.get('confidence', 0.5)) if edge else prediction.get('confidence', 0.5)
        up_price = edge.get('market_up_price', 0) if edge else 0
        down_price = edge.get('market_down_price', 0) if edge else 0

        conn.execute('''
            INSERT INTO realtime_predictions
            (timestamp, btc_price, mid_price, spread, ob_imbalance, cvd, buy_sell_ratio,
             funding_rate, mark_price, direction, confidence, score, signals,
             data_source, cycle, action, edge, our_confidence, up_price, down_price,
             market_slug, market_question)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            prediction.get('timestamp', datetime.now(timezone.utc).isoformat()),
            mid_price,
            mid_price,
            ws_status.get('spread', 0),
            ws_status.get('ob_imbalance', 0),
            ws_status.get('cvd', 0),
            ws_status.get('buy_sell_ratio', 0),
            ws_status.get('funding_rate', 0),
            ws_status.get('mark_price', 0),
            prediction.get('direction', 0),
            prediction.get('confidence', 0.5),
            prediction.get('score', 0),
            json.dumps(prediction.get('signals', []), ensure_ascii=False),
            prediction.get('data_source', 'unknown'),
            self.cycle_count,
            prediction.get('action', ''),
            edge_val,
            our_conf,
            up_price,
            down_price,
            market_slug,
            market_question,
        ))
        conn.commit()
        conn.close()

    def _log_edge(self, edge: dict):
        """Log Polymarket edge to SQLite."""
        if not edge:
            return
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            INSERT INTO polymarket_edges
            (timestamp, market_slug, market_question, market_up_price, market_down_price,
             our_direction, our_confidence, our_prob, market_price, edge, edge_pct,
             ev_per_share, bet_on, action, market_active, market_closed,
             market_volume, market_liquidity, recommendation, cycle)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now(timezone.utc).isoformat(),
            edge.get('market_slug', ''),
            edge.get('market_question', ''),
            edge.get('market_up_price', 0),
            edge.get('market_down_price', 0),
            edge.get('our_direction', ''),
            edge.get('our_confidence', 0.5),
            edge.get('our_prob_for_bet', 0.5),
            edge.get('market_price', 0.5),
            edge.get('edge', 0),
            edge.get('edge_pct', '0%'),
            edge.get('ev_per_share', 0),
            edge.get('bet_on', ''),
            edge.get('action', 'HOLD'),
            1 if edge.get('market_active') else 0,
            1 if edge.get('market_closed') else 0,
            edge.get('market_volume', 0),
            edge.get('market_liquidity', 0),
            edge.get('recommendation', ''),
            self.cycle_count,
        ))
        conn.commit()
        conn.close()

    def run_cycle(self) -> dict:
        """Run a single prediction cycle with Polymarket edge detection."""
        self.cycle_count += 1
        start = time.time()

        # Get WebSocket status
        ws_status = self.ws.get_status()

        # Run strategy prediction
        prediction = self.strategy.predict()

        # Add WebSocket metadata
        prediction['mid_price'] = ws_status.get('mid_price', 0)
        prediction['spread'] = ws_status.get('spread', 0)
        prediction['ob_imbalance'] = ws_status.get('ob_imbalance', 0)
        prediction['cvd'] = ws_status.get('cvd', 0)
        prediction['buy_sell_ratio'] = ws_status.get('buy_sell_ratio', 0)
        prediction['funding_rate'] = ws_status.get('funding_rate', 0)
        prediction['mark_price'] = ws_status.get('mark_price', 0)
        prediction['cycle'] = self.cycle_count

        # Fetch Polymarket market and compute edge
        market = self.poly.get_current_market()
        edge = None
        if market:
            edge = self.poly.compute_edge(prediction, market)
            self.last_edge = edge

        # Log to database
        self._log_prediction(prediction, ws_status)
        self._log_edge(edge)

        duration = time.time() - start
        self.last_prediction = prediction

        # Print summary
        direction = 'UP' if prediction['direction'] == 1 else 'DN'
        conf = prediction['confidence']
        price = ws_status.get('mid_price', 0)

        print(f"\n{'='*70}")
        print(f"🔮 Cycle #{self.cycle_count} | {prediction['timestamp']}")
        print(f"{'='*70}")
        print(f"  💰 BTC: ${price:,.2f} | Spread: ${ws_status.get('spread', 0):.2f}")
        print(f"  🎯 Direction: {direction} ({conf:.1%})")
        print(f"  📊 Score: {prediction['score']:.4f}")
        print(f"  📡 Data: {prediction.get('data_source', 'unknown')} | Cycle: {duration:.2f}s")
        print(f"  📈 OB Imbalance: {ws_status.get('ob_imbalance', 0):.4f}")
        print(f"  💹 CVD: {ws_status.get('cvd', 0):.4f} | B/S Ratio: {ws_status.get('buy_sell_ratio', 0):.4f}")
        print(f"  💰 Funding: {ws_status.get('funding_rate', 0):.6f}")

        # Polymarket edge section
        if edge:
            print(f"\n  📊 Polymarket Edge:")
            print(f"     Market: {edge['market_question'][:60]}")
            print(f"     Market Odds: Up ${edge['market_up_price']:.3f} | Down ${edge['market_down_price']:.3f}")
            print(f"     Our Prediction: {edge['our_direction']} ({edge['our_confidence']:.3f})")
            print(f"     Edge: {edge['edge_pct']} (EV/share: ${edge['ev_per_share']:.4f})")
            print(f"     Action: {edge['action']}")
            print(f"     💡 {edge['recommendation']}")
        else:
            print(f"\n  📊 Polymarket: No active market found")

        print(f"  📡 Signals:")
        for s in prediction.get('signals', []):
            print(f"    - {s}")

        return prediction

    def run_continuous(self):
        """Run prediction loop continuously."""
        logger.info(f"Starting realtime prediction (interval={self.interval}s)")

        # Start WebSocket
        self.ws.start()
        logger.info("WebSocket started, waiting for data...")

        # Wait for initial data
        time.sleep(15)

        ws_status = self.ws.get_status()
        if ws_status['order_book_updates'] == 0:
            logger.warning("No WebSocket data after 15s, will use REST fallback")
            from order_flow import OrderFlowEngine
            self.order_flow = OrderFlowEngine()
            self.strategy.order_flow = self.order_flow
        else:
            logger.info(f"WebSocket ready: {ws_status['order_book_updates']} OB updates, "
                       f"mid=${ws_status['mid_price']:,.2f}")

        self.running = True

        try:
            while self.running:
                try:
                    self.run_cycle()
                except Exception as e:
                    logger.error(f"Cycle error: {e}", exc_info=True)

                # Sleep in small increments for responsive shutdown
                for _ in range(int(self.interval * 10)):
                    if not self.running:
                        break
                    time.sleep(0.1)

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        finally:
            self.stop()

    def stop(self):
        """Stop the prediction service."""
        self.running = False
        self.ws.stop()
        logger.info(f"Service stopped. Total cycles: {self.cycle_count}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Realtime BTC Predictor with Polymarket Edge')
    parser.add_argument('--once', action='store_true', help='Run single prediction')
    parser.add_argument('--interval', type=int, default=30, help='Prediction interval (seconds)')
    args = parser.parse_args()

    predictor = RealtimePredictor(interval=args.interval)

    if args.once:
        predictor.ws.start()
        time.sleep(15)
        result = predictor.run_cycle()
        predictor.ws.stop()
        print(f"\n✅ Single prediction complete.")
    else:
        predictor.run_continuous()


if __name__ == "__main__":
    main()
