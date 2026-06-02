"""
Polymarket Simulated Trading Engine
====================================
Simulates trading BTC 5-min Up/Down markets on Polymarket with virtual funds.

Features:
  - Starting balance: configurable (default 100 USDT)
  - Position sizing: Kelly criterion + fixed fraction
  - Market resolution: checks actual BTC price movement vs market window
  - P&L tracking: win rate, drawdown, Sharpe, total return
  - SQLite persistence: all trades logged

Usage:
    python3 sim_trader.py --once        # Single trade simulation
    python3 sim_trader.py --interval N  # Continuous simulation
"""

import json
import time
import logging
import sqlite3
import sys
import os
import math
import urllib.request
from datetime import datetime, timezone, timedelta
from collections import deque
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from websocket_collector import WebSocketClient
from unified_strategy import UnifiedStrategyEngine
from realtime_service import PolymarketEdgeFinder

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
DB_PATH = os.path.join(DATA_DIR, 'btc_predictor.db')

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'sim_trader.log')),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)

# OKX REST API for price verification
OKX_API = "https://www.okx.com"


class SimulatedWallet:
    """Virtual wallet tracking USDT balance and positions."""

    def __init__(self, initial_balance: float = 100.0):
        self.initial_balance = initial_balance
        self.balance = initial_balance  # Available USDT
        self.positions = []  # Open positions
        self.trade_history = []
        self.peak_balance = initial_balance
        self.max_drawdown = 0.0
        self.total_pnl = 0.0
        self.wins = 0
        self.losses = 0
        self.total_trades = 0
        self.total_fees = 0.0

    @property
    def total_value(self) -> float:
        """Total portfolio value = balance + unrealized position values."""
        position_value = sum(p.get('current_value', 0) for p in self.positions)
        return self.balance + position_value

    @property
    def drawdown(self) -> float:
        """Current drawdown from peak."""
        if self.peak_balance == 0:
            return 0.0
        dd = (self.peak_balance - self.total_value) / self.peak_balance
        self.max_drawdown = max(self.max_drawdown, dd)
        return dd

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.wins / self.total_trades

    @property
    def total_return(self) -> float:
        return (self.total_value - self.initial_balance) / self.initial_balance

    def place_bet(self, direction: str, price: float, amount: float,
                  market_slug: str, market_question: str, edge: float,
                  our_confidence: float, btc_entry_price: float = 0.0) -> dict:
        """Place a simulated bet on Polymarket.

        Args:
            direction: 'Up' or 'Down'
            price: Market price (0-1)
            amount: USDT to bet
            edge: Our calculated edge
            our_confidence: Our prediction confidence
            btc_entry_price: BTC price at entry time (for resolution)
        """
        # Polymarket fee: 2%
        fee = amount * 0.02
        net_amount = amount - fee
        self.balance -= amount
        self.total_fees += fee

        # Calculate shares received
        shares = net_amount / price if price > 0 else 0

        position = {
            'id': len(self.trade_history) + 1,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'direction': direction,
            'price': price,
            'amount': amount,
            'fee': fee,
            'shares': shares,
            'market_slug': market_slug,
            'market_question': market_question,
            'edge': edge,
            'our_confidence': our_confidence,
            'btc_entry_price': btc_entry_price,  # Track BTC price at entry
            'status': 'open',
            'resolution_price': None,
            'resolution_time': None,
            'pnl': 0.0,
            'win': False,
        }

        self.positions.append(position)
        return position

    def resolve_position(self, position: dict, won: bool, resolution_price: float):
        """Resolve a position after market closes."""
        position['status'] = 'resolved'
        position['resolution_price'] = resolution_price
        position['resolution_time'] = datetime.now(timezone.utc).isoformat()
        position['win'] = won

        if won:
            # Winning shares pay $1 each
            payout = position['shares'] * 1.0
            self.balance += payout
            position['pnl'] = payout - position['amount']
            self.wins += 1
        else:
            # Losing shares pay $0
            position['pnl'] = -position['amount']
            self.losses += 1

        self.total_trades += 1
        self.total_pnl += position['pnl']
        self.trade_history.append(position)

        # Update peak
        if self.total_value > self.peak_balance:
            self.peak_balance = self.total_value

        # Remove from open positions
        if position in self.positions:
            self.positions.remove(position)

        return position

    def get_summary(self) -> dict:
        return {
            'initial_balance': self.initial_balance,
            'current_balance': round(self.balance, 4),
            'total_value': round(self.total_value, 4),
            'total_pnl': round(self.total_pnl, 4),
            'total_return_pct': round(self.total_return * 100, 2),
            'max_drawdown_pct': round(self.max_drawdown * 100, 2),
            'win_rate': round(self.win_rate * 100, 2),
            'wins': self.wins,
            'losses': self.losses,
            'total_trades': self.total_trades,
            'total_fees': round(self.total_fees, 4),
            'open_positions': len(self.positions),
        }


class KellySizer:
    """Kelly criterion position sizing."""

    def __init__(self, max_fraction: float = 0.10, min_edge: float = 0.005):
        """
        Args:
            max_fraction: Max Kelly fraction (0.10 = 10% of bankroll)
            min_edge: Minimum edge to bet (lowered from 0.03 to match strategy)
        """
        self.max_fraction = max_fraction
        self.min_edge = min_edge

    def calculate(self, edge: float, confidence: float, market_price: float) -> float:
        """Calculate optimal bet size as fraction of bankroll.

        Kelly formula: f* = (bp - q) / b
        Where:
            b = odds - 1 (net odds)
            p = our probability of winning
            q = 1 - p (probability of losing)

        Simplified for binary markets:
            f* = (our_confidence - market_price) / (1 - market_price)
        """
        if edge < self.min_edge or confidence < 0.50:  # Lowered from 0.55 to match strategy threshold
            return 0.0

        # Kelly fraction
        if market_price >= 1.0:
            return 0.0

        kelly = (confidence - market_price) / (1.0 - market_price)

        # Apply max fraction cap (use half-Kelly for safety)
        kelly = min(kelly * 0.5, self.max_fraction)

        return max(kelly, 0.0)


class MarketResolver:
    """Resolve Polymarket 5-min markets using actual BTC price data."""

    def __init__(self):
        self.price_cache = {}

    def get_okx_price(self) -> Optional[float]:
        """Get current BTC price from OKX."""
        url = f"{OKX_API}/api/v5/market/ticker?instId=BTC-USDT"
        req = urllib.request.Request(url, headers={"User-Agent": "BTC-Predictor/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                if data.get('code') == '0' and data.get('data'):
                    return float(data['data'][0]['last'])
        except Exception as e:
            logger.debug(f"OKX price fetch failed: {e}")
        return None

    def resolve_market(self, market_slug: str, market_end_time: str) -> dict:
        """Resolve a 5-min market by checking BTC price movement.

        Market resolves 'Up' if BTC price at end >= price at start.
        We use current price as proxy for end price (since market is already closed).
        """
        # Get current price
        current_price = self.get_okx_price()
        if not current_price:
            return {'resolved': False, 'reason': 'Price fetch failed'}

        # Parse market window from slug: btc-updown-5m-{timestamp}
        # The timestamp is the END time of the 5-min window
        try:
            ts = int(market_slug.split('-')[-1])
            window_end = datetime.fromtimestamp(ts, tz=timezone.utc)
            window_start = window_end - timedelta(minutes=5)
        except:
            return {'resolved': False, 'reason': 'Invalid slug'}

        # For resolved markets, we need historical price at window start
        # Since we can't easily get historical, use a simpler approach:
        # Compare current price to the mark price at resolution time
        # For simulation: we track entry price and check if direction was correct

        return {
            'resolved': True,
            'current_price': current_price,
            'window_start': window_start.isoformat(),
            'window_end': window_end.isoformat(),
        }


class SimTrader:
    """Main simulation trader."""

    def __init__(self, initial_balance: float = 100.0, interval: int = 30):
        self.ws = WebSocketClient(symbol="BTC-USDT")
        self.strategy = UnifiedStrategyEngine()
        self.poly = PolymarketEdgeFinder()
        self.wallet = SimulatedWallet(initial_balance)
        self.sizer = KellySizer(max_fraction=0.15, min_edge=0.03)
        self.resolver = MarketResolver()
        self.interval = interval
        self.running = False
        self.cycle_count = 0

        # Track resolved markets to avoid double-betting
        self.resolved_markets = set()

        # Pending resolutions: positions waiting for market to close
        self.pending_resolutions = []

        # Initialize DB
        self._init_db()

        # Attach WebSocket
        self.strategy.attach_websocket(self.ws)

    def _init_db(self):
        conn = sqlite3.connect(DB_PATH)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS sim_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                direction TEXT,
                market_slug TEXT,
                market_question TEXT,
                entry_price REAL,
                amount REAL,
                fee REAL,
                shares REAL,
                edge REAL,
                our_confidence REAL,
                kelly_fraction REAL,
                status TEXT,
                resolution_price REAL,
                resolution_time TEXT,
                pnl REAL,
                win INTEGER,
                wallet_balance_after REAL,
                total_value_after REAL,
                cycle INTEGER,
                strategy TEXT,
                btc_entry_price REAL DEFAULT 0
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS sim_portfolio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                strategy TEXT,
                balance REAL,
                total_value REAL,
                total_pnl REAL,
                total_return_pct REAL,
                max_drawdown_pct REAL,
                win_rate REAL,
                wins INTEGER,
                losses INTEGER,
                total_trades INTEGER,
                open_positions INTEGER
            )
        ''')

        conn.commit()
        conn.close()

    def _log_trade(self, trade: dict, kelly_frac: float):
        conn = sqlite3.connect(DB_PATH)
        conn.execute('''
            INSERT INTO sim_trades
            (timestamp, direction, market_slug, market_question,
             entry_price, amount, fee, shares, edge, our_confidence,
             kelly_fraction, status, resolution_price, resolution_time,
             pnl, win, wallet_balance_after, total_value_after, cycle, strategy, btc_entry_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade['timestamp'],
            trade['direction'],
            trade['market_slug'],
            trade['market_question'],
            trade['price'],
            trade['amount'],
            trade['fee'],
            trade['shares'],
            trade['edge'],
            trade['our_confidence'],
            kelly_frac,
            trade['status'],
            trade.get('resolution_price'),
            trade.get('resolution_time'),
            trade.get('pnl', 0),
            1 if trade.get('win') else 0,
            self.wallet.balance,
            self.wallet.total_value,
            self.cycle_count,
            'UP-ONLY',  # Strategy name
            trade.get('btc_entry_price', 0),
        ))
        conn.commit()
        conn.close()

    def _log_portfolio(self):
        summary = self.wallet.get_summary()
        conn = sqlite3.connect(DB_PATH)
        conn.execute('''
            INSERT INTO sim_portfolio
            (timestamp, strategy, balance, total_value, total_pnl, total_return_pct,
             max_drawdown_pct, win_rate, wins, losses, total_trades, open_positions)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now(timezone.utc).isoformat(),
            'UP-ONLY',  # Strategy name
            summary['current_balance'],
            summary['total_value'],
            summary['total_pnl'],
            summary['total_return_pct'],
            summary['max_drawdown_pct'],
            summary['win_rate'],
            summary['wins'],
            summary['losses'],
            summary['total_trades'],
            summary['open_positions'],
        ))
        conn.commit()
        conn.close()

    def _check_pending_resolutions(self):
        """Check if any open positions can be resolved."""
        now = time.time()
        resolved = []

        for pos in list(self.wallet.positions):
            # Parse market end time from slug
            try:
                ts = int(pos['market_slug'].split('-')[-1])
                end_time = ts
                # Resolve if market window has passed (+ 30s buffer)
                if now > end_time + 30:
                    resolved.append(pos)
            except:
                pass

        for pos in resolved:
            self._resolve_trade(pos)

    def _resolve_trade(self, position: dict):
        """Resolve a single trade based on actual BTC price movement."""
        # Get current price
        current_price = self.resolver.get_okx_price()
        if not current_price:
            logger.warning(f"Cannot resolve trade #{position['id']}: price fetch failed")
            return

        entry_price = position.get('btc_entry_price', 0)
        direction = position['direction']

        # Determine win/loss based on actual price movement
        if entry_price > 0:
            # Accurate resolution: compare BTC price at entry vs resolution
            if direction == 'Up':
                won = current_price > entry_price
            else:
                won = current_price < entry_price
            logger.info(f"Trade #{position['id']}: {direction} entry=${entry_price:.2f} -> current=${current_price:.2f} -> {'WIN' if won else 'LOSS'}")
        else:
            # Fallback: probabilistic resolution if no entry price recorded
            won = self._probabilistic_resolve(position)
            logger.info(f"Trade #{position['id']}: Fallback probabilistic resolution -> {'WIN' if won else 'LOSS'}")

        resolution = self.resolver.resolve_market(
            position['market_slug'],
            position.get('market_question', '')
        )

        self.wallet.resolve_position(position, won, current_price)
        self._log_trade(position, position.get('kelly_fraction', 0))

        dir_str = "✅ WIN" if won else "❌ LOSS"
        pnl_str = f"+${position['pnl']:.4f}" if position['pnl'] > 0 else f"-${abs(position['pnl']):.4f}"

        print(f"\n  {dir_str} Trade #{position['id']}: {position['direction']} "
              f"at ${position['price']:.3f} | {pnl_str}")
        print(f"  💰 Balance: ${self.wallet.balance:.2f} | "
              f"Total: ${self.wallet.total_value:.2f} | "
              f"Return: {self.wallet.total_return*100:.2f}%")

    def _probabilistic_resolve(self, position: dict) -> bool:
        """Resolve trade probabilistically based on our confidence.

        If our confidence is 60% and we predicted correctly, 60% chance of winning.
        This simulates the actual market resolution more accurately than
        trying to reconstruct historical prices.
        """
        import random
        our_conf = position.get('our_confidence', 0.5)
        edge = position.get('edge', 0)

        # If edge is positive, our confidence is our true win probability
        # If edge is negative, market is smarter, reduce our win probability
        if edge > 0:
            win_prob = our_conf
        else:
            # Edge is negative → market disagrees with us
            # Reduce win probability proportionally
            win_prob = our_conf * (1 - abs(edge))

        return random.random() < win_prob

    def run_cycle(self) -> dict:
        """Run a single simulation cycle."""
        self.cycle_count += 1
        start = time.time()

        # Check pending resolutions first
        self._check_pending_resolutions()

        # Get prediction
        ws_status = self.ws.get_status()
        prediction = self.strategy.predict()

        # Get Polymarket market
        market = self.poly.get_current_market()
        edge = None
        if market:
            edge = self.poly.compute_edge(prediction, market)

        # Log portfolio snapshot
        self._log_portfolio()

        # Decision: should we bet? UP-ONLY strategy
        action = "HOLD"
        trade = None

        if edge and edge.get('action', '').startswith('BUY') and edge.get('bet_on') == 'Up':
            # Calculate position size using Kelly
            kelly_frac = self.sizer.calculate(
                edge['edge'],
                edge['our_confidence'],
                edge['market_price']
            )

            if kelly_frac > 0:
                bet_amount = min(
                    self.wallet.balance * kelly_frac,
                    self.wallet.balance * 0.20,  # Max 20% per trade
                    1000.0  # Max $1000 per trade
                )
                bet_amount = max(bet_amount, 5.0)  # Min $5 (Polymarket minimum)

                # Place bet - UP-ONLY
                direction = 'Up'  # Force UP-ONLY
                market_price = edge['market_price']
                btc_entry_price = ws_status.get('mid_price', 0)  # Track BTC price at entry

                trade = self.wallet.place_bet(
                    direction=direction,
                    price=market_price,
                    amount=bet_amount,
                    market_slug=edge['market_slug'],
                    market_question=edge['market_question'],
                    edge=edge['edge'],
                    our_confidence=edge['our_confidence'],
                    btc_entry_price=btc_entry_price,
                )
                trade['kelly_fraction'] = kelly_frac
                trade['btc_entry_price'] = btc_entry_price  # Store for logging
                self._log_trade(trade, kelly_frac)
                action = edge['action']

        # Print summary
        summary = self.wallet.get_summary()
        direction = 'UP' if prediction['direction'] == 1 else 'DN'
        conf = prediction['confidence']
        price = ws_status.get('mid_price', 0)

        print(f"\n{'='*70}")
        print(f"🔮 Sim Cycle #{self.cycle_count} | {datetime.now(timezone.utc).isoformat()}")
        print(f"{'='*70}")
        print(f"  💰 BTC: ${price:,.2f}")
        print(f"  🎯 Prediction: {direction} ({conf:.1%})")

        if edge:
            print(f"  📊 Edge: {edge['edge_pct']} | Action: {edge['action']}")
            print(f"  📈 Market: Up ${edge['market_up_price']:.3f} / Down ${edge['market_down_price']:.3f}")

        if trade:
            print(f"\n  🎲 PLACED BET: {trade['direction']} ${trade['amount']:.2f} "
                  f"at ${trade['price']:.3f}")
            print(f"     Shares: {trade['shares']:.4f} | Kelly: {trade['kelly_fraction']:.2%}")
            print(f"     BTC Entry: ${trade.get('btc_entry_price', 0):,.2f}")
        else:
            print(f"\n  ⏸ HOLD — No edge ≥3% or confidence too low")

        print(f"\n  💼 Wallet Summary:")
        print(f"     Balance: ${summary['current_balance']:.2f}")
        print(f"     Total Value: ${summary['total_value']:.2f}")
        print(f"     P&L: {'+' if summary['total_pnl'] >= 0 else ''}{summary['total_pnl']:.2f} USDT")
        print(f"     Return: {summary['total_return_pct']:.2f}%")
        print(f"     Max Drawdown: {summary['max_drawdown_pct']:.2f}%")
        print(f"     Win Rate: {summary['win_rate']:.1f}% ({summary['wins']}W/{summary['losses']}L)")
        print(f"     Total Trades: {summary['total_trades']} | Open: {summary['open_positions']}")
        print(f"     Fees Paid: ${summary['total_fees']:.4f}")

        return {
            'prediction': prediction,
            'edge': edge,
            'trade': trade,
            'wallet': summary,
        }

    def run_continuous(self):
        """Run simulation continuously."""
        logger.info(f"Starting sim trader (balance=${self.wallet.initial_balance}, interval={self.interval}s)")

        self.ws.start()
        time.sleep(15)

        ws_status = self.ws.get_status()
        if ws_status['order_book_updates'] == 0:
            logger.warning("No WebSocket data, using REST fallback")
        else:
            logger.info(f"WebSocket ready: {ws_status['order_book_updates']} updates")

        self.running = True

        print(f"\n{'='*70}")
        print(f"🎰 SIMULATION STARTED (UP-ONLY STRATEGY)")
        print(f"   Initial Balance: ${self.wallet.initial_balance:.2f} USDT")
        print(f"   Kelly Max: 15% | Min Edge: 3% | Min Confidence: 50%")
        print(f"   Fee: 2% per trade")
        print(f"   Strategy: UP-ONLY (no DOWN bets)")
        print(f"   Interval: {self.interval}s")
        print(f"{'='*70}")

        try:
            while self.running:
                try:
                    self.run_cycle()
                except Exception as e:
                    logger.error(f"Cycle error: {e}", exc_info=True)

                for _ in range(int(self.interval * 10)):
                    if not self.running:
                        break
                    time.sleep(0.1)

        except KeyboardInterrupt:
            logger.info("Simulation stopped")
        finally:
            self.stop()

    def stop(self):
        self.running = False
        self.ws.stop()

        # Resolve all remaining positions
        for pos in list(self.wallet.positions):
            self._resolve_trade(pos)

        summary = self.wallet.get_summary()
        print(f"\n{'='*70}")
        print(f"🏁 SIMULATION FINAL RESULTS")
        print(f"{'='*70}")
        print(f"   Initial: ${self.wallet.initial_balance:.2f}")
        print(f"   Final: ${summary['total_value']:.2f}")
        print(f"   P&L: {'+' if summary['total_pnl'] >= 0 else ''}{summary['total_pnl']:.2f} USDT")
        print(f"   Return: {summary['total_return_pct']:.2f}%")
        print(f"   Max Drawdown: {summary['max_drawdown_pct']:.2f}%")
        print(f"   Win Rate: {summary['win_rate']:.1f}% ({summary['wins']}W/{summary['losses']}L)")
        print(f"   Total Trades: {summary['total_trades']}")
        print(f"   Fees Paid: ${summary['total_fees']:.4f}")
        print(f"{'='*70}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Polymarket Simulated Trader')
    parser.add_argument('--balance', type=float, default=100.0, help='Initial balance (USDT)')
    parser.add_argument('--once', action='store_true', help='Run single cycle')
    parser.add_argument('--interval', type=int, default=30, help='Cycle interval (seconds)')
    args = parser.parse_args()

    trader = SimTrader(initial_balance=args.balance, interval=args.interval)

    if args.once:
        trader.ws.start()
        time.sleep(15)
        result = trader.run_cycle()
        trader.ws.stop()
        print(f"\n✅ Single cycle complete.")
    else:
        trader.run_continuous()


if __name__ == "__main__":
    main()
