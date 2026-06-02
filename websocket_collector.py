"""
WebSocket Real-time Data Collector
====================================
Subscribes to OKX WebSocket streams for:
  - Order book depth (top 5 levels, books5 channel)
  - Real-time trades (tick-by-tick)
  - Mark price & funding rate (for Order Flow module)

OKX format:
  books5: bids/asks = [[price, qty, _, _], ...]
  trades: {px, sz, side, ts, ...}
  mark-price: {markPx, ts}
  funding-rate: {fundingRate, nextFundingTime, ts}
"""

import json
import time
import threading
import logging
from collections import deque
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class OrderBookSnapshot:
    """Thread-safe order book snapshot"""
    def __init__(self, max_depth=20):
        self.max_depth = max_depth
        self.bids = deque(maxlen=max_depth)  # [(price, qty), ...]
        self.asks = deque(maxlen=max_depth)
        self.timestamp = 0
        self.lock = threading.Lock()
        self.update_count = 0

    def update(self, bids: List[Tuple[float, float]], asks: List[Tuple[float, float]]):
        with self.lock:
            self.bids = deque(sorted(bids, key=lambda x: -x[0])[:self.max_depth])
            self.asks = deque(sorted(asks, key=lambda x: x[0])[:self.max_depth])
            self.timestamp = time.time()
            self.update_count += 1

    def get_snapshot(self) -> Dict:
        with self.lock:
            return {
                'bids': list(self.bids),
                'asks': list(self.asks),
                'timestamp': self.timestamp,
                'update_count': self.update_count,
            }

    def get_imbalance(self, levels=5) -> float:
        """Order book imbalance: (bid_vol - ask_volume) / (bid_vol + ask_volume)
        >0 means more buy pressure, <0 means more sell pressure"""
        with self.lock:
            bid_vol = sum(q for _, q in list(self.bids)[:levels])
            ask_vol = sum(q for _, q in list(self.asks)[:levels])
            total = bid_vol + ask_vol
            if total == 0:
                return 0.0
            return (bid_vol - ask_vol) / total

    def get_spread(self) -> float:
        """Bid-ask spread"""
        with self.lock:
            if self.bids and self.asks:
                return self.asks[0][0] - self.bids[0][0]
            return 0.0

    def get_mid_price(self) -> float:
        """Mid price"""
        with self.lock:
            if self.bids and self.asks:
                return (self.bids[0][0] + self.asks[0][0]) / 2
            return 0.0

    def get_weighted_imbalance(self) -> float:
        """Weighted imbalance: closer levels have more weight"""
        with self.lock:
            bid_w = sum(q * (1.0 / (i + 1)) for i, (_, q) in enumerate(list(self.bids)[:5]))
            ask_w = sum(q * (1.0 / (i + 1)) for i, (_, q) in enumerate(list(self.asks)[:5]))
            total = bid_w + ask_w
            if total == 0:
                return 0.0
            return (bid_w - ask_w) / total


class TradeStream:
    """Real-time trade flow accumulator"""
    def __init__(self, window_sec=300):
        self.window_sec = window_sec
        self.trades = deque()  # [(timestamp, price, qty, is_buyer_maker), ...]
        self.lock = threading.Lock()
        self.total_buy_volume = 0.0
        self.total_sell_volume = 0.0

    def add_trade(self, timestamp: float, price: float, qty: float, is_buyer_maker: bool):
        """Add a trade. is_buyer_maker=True means sell-initiated (taker sold)"""
        with self.lock:
            self.trades.append((timestamp, price, qty, is_buyer_maker))
            if is_buyer_maker:
                self.total_sell_volume += qty
            else:
                self.total_buy_volume += qty
            # Clean old trades
            cutoff = timestamp - self.window_sec
            while self.trades and self.trades[0][0] < cutoff:
                old = self.trades.popleft()
                if old[3]:
                    self.total_sell_volume -= old[2]
                else:
                    self.total_buy_volume -= old[2]

    def get_cvd(self) -> float:
        """Cumulative Volume Delta: buy_vol - sell_vol"""
        with self.lock:
            return self.total_buy_volume - self.total_sell_volume

    def get_buy_sell_ratio(self) -> float:
        """Buy volume / Sell volume"""
        with self.lock:
            if self.total_sell_volume == 0:
                return float('inf') if self.total_buy_volume > 0 else 1.0
            return self.total_buy_volume / self.total_sell_volume

    def get_recent_trades(self, seconds: float = 60) -> List:
        """Get trades in last N seconds"""
        with self.lock:
            cutoff = time.time() - seconds
            return [(t, p, q, m) for t, p, q, m in self.trades if t >= cutoff]

    def get_trade_rate(self, seconds: float = 60) -> int:
        """Number of trades per second in last N seconds"""
        trades = self.get_recent_trades(seconds)
        return len(trades) / seconds if seconds > 0 else 0

    def get_large_trades(self, threshold_btc=0.1, seconds=60) -> List:
        """Get large trades (> threshold BTC) in last N seconds"""
        trades = self.get_recent_trades(seconds)
        return [(t, p, q, m) for t, p, q, m in trades if q >= threshold_btc]


class WebSocketClient:
    """OKX WebSocket client for BTC real-time data"""

    OKX_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"

    def __init__(self, symbol="BTC-USDT"):
        self.symbol = symbol
        self.ws = None
        self.ws_thread = None
        self.running = False
        self.order_book = OrderBookSnapshot(max_depth=20)
        self.trade_stream = TradeStream(window_sec=300)
        self.funding_rate = 0.0
        self.next_funding_time = 0
        self.mark_price = 0.0
        self.last_heartbeat = time.time()
        self._callbacks = []
        self._reconnect_delay = 5
        self._max_reconnect = 30

    def on_update(self, callback):
        """Register callback for data updates: callback(data_type, data)"""
        self._callbacks.append(callback)

    def _notify(self, data_type: str, data: Dict):
        for cb in self._callbacks:
            try:
                cb(data_type, data)
            except Exception as e:
                logger.error(f"Callback error: {e}")

    def _parse_books5(self, msg: Dict):
        """Parse OKX books5 order book data"""
        try:
            data = msg.get('data', [{}])[0]
            # OKX format: [[price, qty, _, _], ...]
            bids = [(float(row[0]), float(row[1])) for row in data.get('bids', [])[:20]]
            asks = [(float(row[0]), float(row[1])) for row in data.get('asks', [])[:20]]
            self.order_book.update(bids, asks)
            self._notify('orderbook', {
                'bids': bids,
                'asks': asks,
                'imbalance': self.order_book.get_imbalance(),
                'weighted_imbalance': self.order_book.get_weighted_imbalance(),
                'spread': self.order_book.get_spread(),
                'mid_price': self.order_book.get_mid_price(),
            })
        except Exception as e:
            logger.error(f"books5 parse error: {e}")

    def _parse_trades(self, msg: Dict):
        """Parse OKX trade data"""
        try:
            for d in msg.get('data', []):
                ts = float(d['ts']) / 1000.0
                price = float(d['px'])
                qty = float(d['sz'])
                # OKX: side='buy' means buyer-initiated (taker bought)
                #      side='sell' means seller-initiated (taker sold)
                is_buyer_maker = (d.get('side') == 'sell')
                self.trade_stream.add_trade(ts, price, qty, is_buyer_maker)
                self._notify('trade', {
                    'price': price,
                    'qty': qty,
                    'side': d.get('side'),
                    'is_buyer_maker': is_buyer_maker,
                    'cvd': self.trade_stream.get_cvd(),
                    'buy_sell_ratio': self.trade_stream.get_buy_sell_ratio(),
                })
        except Exception as e:
            logger.error(f"trades parse error: {e}")

    def _parse_mark_price(self, msg: Dict):
        """Parse OKX mark price"""
        try:
            for d in msg.get('data', []):
                self.mark_price = float(d.get('markPx', 0))
                self._notify('mark_price', {
                    'mark_price': self.mark_price,
                })
        except Exception as e:
            logger.error(f"mark-price parse error: {e}")

    def _parse_funding_rate(self, msg: Dict):
        """Parse OKX funding rate"""
        try:
            for d in msg.get('data', []):
                self.funding_rate = float(d.get('fundingRate', 0))
                self.next_funding_time = int(d.get('nextFundingTime', 0))
                self._notify('funding', {
                    'funding_rate': self.funding_rate,
                    'next_funding_time': self.next_funding_time,
                })
        except Exception as e:
            logger.error(f"funding-rate parse error: {e}")

    def _on_message(self, ws, message):
        try:
            msg = json.loads(message)

            # Heartbeat / pong
            if 'pong' in msg or msg.get('event') == 'pong':
                self.last_heartbeat = time.time()
                return

            # Subscribe confirmation
            if msg.get('event') == 'subscribe':
                return

            # Error
            if msg.get('event') == 'error':
                logger.error(f"OKX WS error: {msg}")
                return

            # Data messages have 'arg' with channel info
            arg = msg.get('arg', {})
            channel = arg.get('channel', '')

            if channel == 'books5':
                self._parse_books5(msg)
            elif channel == 'trades':
                self._parse_trades(msg)
            elif channel == 'mark-price':
                self._parse_mark_price(msg)
            elif channel == 'funding-rate':
                self._parse_funding_rate(msg)
            elif 'bids' in str(msg.get('data', '')):
                # Fallback: try books5 parse
                self._parse_books5(msg)

            self.last_heartbeat = time.time()

        except json.JSONDecodeError:
            pass
        except Exception as e:
            logger.error(f"WS message error: {e}")

    def _connect(self):
        """Connect to OKX WebSocket and subscribe"""
        import websocket as ws_lib

        def on_open(ws):
            logger.info("OKX WebSocket connected")
            subscribe_msg = {
                "op": "subscribe",
                "args": [
                    {"channel": "books5", "instId": self.symbol},
                    {"channel": "trades", "instId": self.symbol},
                    {"channel": "mark-price", "instId": f"{self.symbol}-SWAP"},
                    {"channel": "funding-rate", "instId": f"{self.symbol}-SWAP"},
                ]
            }
            ws.send(json.dumps(subscribe_msg))
            logger.info("Subscribed to OKX streams: books5, trades, mark-price, funding-rate")

        ws_app = ws_lib.WebSocketApp(
            self.OKX_WS_URL,
            on_message=self._on_message,
            on_open=on_open,
            on_error=lambda ws, err: logger.error(f"OKX WS error: {err}"),
            on_close=lambda ws, close_code, close_msg: logger.warning(f"OKX WS closed: {close_code}"),
        )
        self.ws = ws_app
        ws_app.run_forever(ping_interval=25, ping_timeout=20)

    def start(self):
        """Start WebSocket connection in background thread"""
        if self.running:
            return

        self.running = True
        self._reconnect_delay = 5

        def run_with_reconnect():
            while self.running:
                try:
                    self._connect()
                except Exception as e:
                    logger.error(f"WS connection error: {e}")
                if self.running:
                    delay = min(self._reconnect_delay, self._max_reconnect)
                    logger.info(f"Reconnecting in {delay}s...")
                    time.sleep(delay)
                    self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect)

        self.ws_thread = threading.Thread(target=run_with_reconnect, daemon=True)
        self.ws_thread.start()
        logger.info(f"WebSocket started: OKX ({self.symbol})")

    def stop(self):
        """Stop WebSocket connection"""
        self.running = False
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
        if self.ws_thread:
            self.ws_thread.join(timeout=5)
        logger.info("WebSocket stopped")

    def get_order_book(self) -> OrderBookSnapshot:
        return self.order_book

    def get_trade_stream(self) -> TradeStream:
        return self.trade_stream

    def get_status(self) -> Dict:
        return {
            'exchange': 'OKX',
            'symbol': self.symbol,
            'running': self.running,
            'last_heartbeat': self.last_heartbeat,
            'seconds_since_update': time.time() - self.last_heartbeat,
            'order_book_updates': self.order_book.update_count,
            'mid_price': self.order_book.get_mid_price(),
            'spread': self.order_book.get_spread(),
            'ob_imbalance': self.order_book.get_imbalance(),
            'ob_weighted_imbalance': self.order_book.get_weighted_imbalance(),
            'cvd': self.trade_stream.get_cvd(),
            'buy_sell_ratio': self.trade_stream.get_buy_sell_ratio(),
            'funding_rate': self.funding_rate,
            'mark_price': self.mark_price,
        }


# ============================================================================
# Singleton instance for global access
# ============================================================================
_ws_instance = None
_ws_lock = threading.Lock()


def get_ws_client(symbol="BTC-USDT") -> WebSocketClient:
    """Get or create global WebSocket client instance"""
    global _ws_instance
    with _ws_lock:
        if _ws_instance is None:
            _ws_instance = WebSocketClient(symbol=symbol)
        return _ws_instance


def start_ws(symbol="BTC-USDT") -> WebSocketClient:
    """Start WebSocket client (singleton)"""
    client = get_ws_client(symbol)
    client.start()
    return client


# Quick test
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    def on_data(data_type, data):
        if data_type == 'orderbook':
            print(f"📊 OB: mid={data['mid_price']:.2f} spread={data['spread']:.2f} "
                  f"imb={data['imbalance']:.4f} wimb={data['weighted_imbalance']:.4f}")
        elif data_type == 'trade':
            print(f"💹 Trade: price={data['price']:.1f} cvd={data['cvd']:.4f} "
                  f"ratio={data['buy_sell_ratio']:.4f}")
        elif data_type == 'funding':
            print(f"💰 Funding: rate={data['funding_rate']:.6f}")
        elif data_type == 'mark_price':
            print(f"📈 Mark: {data['mark_price']:.1f}")

    client = WebSocketClient(symbol="BTC-USDT")
    client.on_update(on_data)
    client.start()

    print("Connected to OKX WebSocket. Press Ctrl+C to stop...")
    try:
        while True:
            time.sleep(5)
            status = client.get_status()
            print(f"\n📈 Status: {json.dumps(status, indent=2)}\n")
    except KeyboardInterrupt:
        client.stop()
        print("Done.")
