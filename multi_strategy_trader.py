"""
Unified BTC Trader - Optimized with risk management
===================================================
Bets UP or DOWN based on unified prediction indicators.
Features: Kelly sizing, drawdown circuit breaker, consecutive loss pause.

Usage:
    python3 multi_strategy_trader.py --interval 30
    python3 multi_strategy_trader.py --once
    python3 multi_strategy_trader.py --balance 200
"""

import json
import time
import logging
import sqlite3
import sys
import os
import random
import threading
from datetime import datetime, timezone, timedelta
from collections import deque
from typing import Optional, Dict, List

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
        logging.FileHandler(os.path.join(LOG_DIR, 'multi_strategy.log')),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)

OKX_API = "https://www.okx.com"


# ============================================================================
# Risk Manager
# ============================================================================

class RiskManager:
    """Centralized risk management."""

    def __init__(
        self,
        max_drawdown_pct: float = 0.30,     # Halt if drawdown exceeds 30%
        max_consecutive_losses: int = 6,     # Pause after N consecutive losses
        pause_cycles: int = 10,              # Pause for N cycles
        max_daily_loss_pct: float = 0.15,    # Max daily loss
        max_position_pct: float = 0.20,      # Max 20% per position
        min_position: float = 5.0,           # Minimum bet size
    ):
        self.max_drawdown_pct = max_drawdown_pct
        self.max_consecutive_losses = max_consecutive_losses
        self.pause_cycles = pause_cycles
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_position_pct = max_position_pct
        self.min_position = min_position

        self.consecutive_losses = 0
        self.consecutive_wins = 0
        self.paused_until = 0
        self.daily_pnl = 0.0
        self.daily_start_balance = None
        self.last_day = None
        self.halted = False
        self.halt_reason = ""

    def check(self, wallet) -> tuple:
        """Check all risk limits. Returns (allowed: bool, reason: str)."""
        # Check if halted
        if self.halted:
            return False, f"HALTED: {self.halt_reason}"

        # Check if paused
        if time.time() < self.paused_until:
            remaining = int(self.paused_until - time.time())
            return False, f"PAUSED: {remaining}s remaining (consecutive losses)"

        # Check drawdown
        if wallet.drawdown >= self.max_drawdown_pct:
            self.halted = True
            self.halt_reason = f"Max drawdown {wallet.drawdown*100:.1f}% >= {self.max_drawdown_pct*100:.0f}%"
            return False, self.halt_reason

        # Check daily loss
        today = datetime.now(timezone.utc).date()
        if self.last_day != today:
            self.daily_pnl = 0.0
            self.daily_start_balance = wallet.total_value
            self.last_day = today

        if self.daily_start_balance and self.daily_start_balance > 0:
            daily_loss_pct = -self.daily_pnl / self.daily_start_balance
            if daily_loss_pct >= self.max_daily_loss_pct:
                self.halted = True
                self.halt_reason = f"Max daily loss {daily_loss_pct*100:.1f}% >= {self.max_daily_loss_pct*100:.0f}%"
                return False, self.halt_reason

        return True, "OK"

    def record_trade(self, won: bool, pnl: float):
        """Record trade outcome for risk tracking."""
        if won:
            self.consecutive_losses = 0
            self.consecutive_wins += 1
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0

        self.daily_pnl += pnl

        if self.consecutive_losses >= self.max_consecutive_losses:
            self.paused_until = time.time() + self.pause_cycles * 30
            logger.warning(
                f"⚠️ {self.consecutive_losses} consecutive losses — "
                f"pausing for {self.pause_cycles} cycles"
            )

    def reset_halt(self):
        """Manual reset of halt state."""
        self.halted = False
        self.halt_reason = ""
        self.consecutive_losses = 0


# ============================================================================
# Wallet
# ============================================================================

class Wallet:
    """Single wallet for unified strategy."""

    def __init__(self, initial_balance: float = 100.0):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.positions = []
        self.trade_history = []
        self.peak_balance = initial_balance
        self.max_drawdown = 0.0
        self.total_pnl = 0.0
        self.wins = 0
        self.losses = 0
        self.total_trades = 0
        self.total_fees = 0.0
        self.lock = threading.Lock()

    @property
    def total_value(self) -> float:
        position_value = sum(p.get('current_value', 0) for p in self.positions)
        return self.balance + position_value

    @property
    def drawdown(self) -> float:
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
                  our_confidence: float, kelly_fraction: float, btc_price: float) -> dict:
        with self.lock:
            fee = amount * 0.02
            net_amount = amount - fee
            self.balance -= amount
            self.total_fees += fee

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
                'kelly_fraction': kelly_fraction,
                'btc_entry_price': btc_price,
                'status': 'open',
                'resolution_price': None,
                'resolution_time': None,
                'pnl': 0.0,
                'win': False,
            }

            self.positions.append(position)
            return position

    def resolve_position(self, position: dict, won: bool):
        with self.lock:
            position['status'] = 'resolved'
            position['resolution_time'] = datetime.now(timezone.utc).isoformat()
            position['win'] = won

            if won:
                payout = position['shares'] * 1.0
                self.balance += payout
                position['pnl'] = payout - position['amount']
                self.wins += 1
            else:
                position['pnl'] = -position['amount']
                self.losses += 1

            self.total_trades += 1
            self.total_pnl += position['pnl']
            self.trade_history.append(position)

            if self.total_value > self.peak_balance:
                self.peak_balance = self.total_value

            if position in self.positions:
                self.positions.remove(position)

            return position

    def get_summary(self) -> dict:
        with self.lock:
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


# ============================================================================
# Kelly Sizer (correct formula)
# ============================================================================

class KellySizer:
    """Kelly criterion position sizing for binary prediction markets.

    For Polymarket: you buy YES at price m, get $1 if correct, $0 if wrong.
    Kelly fraction: f = (p - m) / (1 - m)  where p = our probability.
    """

    def __init__(self, max_fraction: float = 0.15, min_edge: float = 0.01,
                 half_kelly: bool = True):
        self.max_fraction = max_fraction
        self.min_edge = min_edge
        self.half_kelly = half_kelly

    def calculate(self, edge: float, confidence: float, market_price: float) -> float:
        """Kelly fraction for binary market.

        f* = (confidence - market_price) / (1 - market_price)
        """
        if edge < self.min_edge or confidence < 0.52:
            return 0.0

        if market_price >= 0.99 or market_price <= 0.01:
            return 0.0

        # Correct Kelly formula for binary markets
        kelly = (confidence - market_price) / (1.0 - market_price)

        # Half-Kelly for conservative sizing
        if self.half_kelly:
            kelly *= 0.5

        kelly = min(kelly, self.max_fraction)
        return max(kelly, 0.0)


# ============================================================================
# Market Resolver
# ============================================================================

class MarketResolver:
    """Resolve Polymarket 5-min BTC markets using OKX candle data.

    Polymarket rule: Up if BTC price at window END >= price at window START.
    We fetch the exact 5-min candle to get open (start) and close (end) prices.
    """

    def __init__(self):
        self.price_cache = {}
        self.window_data = {}  # slug -> {'open': float, 'close': float}

    def _get_window_candle(self, slug: str) -> Optional[dict]:
        """Fetch the 5-minute OKX candle for a given market slug.
        
        Slug format: btc-updown-5m-{end_ts}
        Returns {'open': float, 'close': float} or None.
        """
        import re, urllib.request, time
        m = re.search(r'(\d{9,11})$', slug)
        if not m:
            return None
        
        end_ts = int(m.group(1))
        start_ts = end_ts - 300  # window start = end - 5 min
        # OKX 'after' returns candles with ts < after_ms (older), newest first.
        # To get the exact candle at start_ts, use after = start_ts*1000 + 1
        after_ms = start_ts * 1000 + 1
        url = f"{OKX_API}/api/v5/market/candles?instId=BTC-USDT&bar=5m&after={after_ms}&limit=1"
        req = urllib.request.Request(url, headers={"User-Agent": "BTC-Predictor/1.0"})
        
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                if data.get('code') == '0' and data.get('data'):
                    candle = data['data'][0]
                    # OKX candle format: [ts, open, high, low, close, vol, volCcy, ...]
                    open_price = float(candle[1])
                    close_price = float(candle[4])
                    return {'open': open_price, 'close': close_price}
        except Exception as e:
            logger.debug(f"OKX candle fetch failed for {slug}: {e}")
        
        return None

    def set_entry_price(self, slug: str, price: float):
        """Deprecated — no longer used. Window start/end prices come from candles."""
        pass

    def get_okx_price(self) -> Optional[float]:
        """Get current BTC price from OKX (fallback)."""
        import urllib.request
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

    def resolve_trade(self, position: dict) -> tuple:
        """Resolve a trade using the window's actual start/end BTC prices.
        
        Returns (won: bool, settlement_price: float, source: str).
        """
        slug = position['market_slug']
        direction = position['direction']
        btc_entry = position.get('btc_entry_price', 0)

        # Try to get the window candle (open = start, close = end)
        candle = self._get_window_candle(slug)
        
        if candle:
            open_price = candle['open']
            close_price = candle['close']
            
            # Polymarket rule: Up if close >= open, Down if close < open
            actual_up = close_price >= open_price
            won = (direction == 'Up' and actual_up) or (direction == 'Down' and not actual_up)
            
            source = f"okx_candle (open={open_price:.1f} close={close_price:.1f})"
            # Store window data for reference
            self.window_data[slug] = candle
            
            return won, close_price, source
        
        # Fallback: use current OKX price if candle not yet available
        current_price = self.get_okx_price()
        if not current_price or not btc_entry:
            return self._probabilistic_resolve(position), 0, "probabilistic"
        
        # Last resort: compare against entry price (approximate)
        if direction == 'Up':
            won = current_price >= btc_entry
        else:
            won = current_price <= btc_entry

        return won, current_price, "okx_live_fallback"

    def _probabilistic_resolve(self, position: dict) -> bool:
        """Fallback: use our confidence as win probability."""
        our_conf = position.get('our_confidence', 0.5)
        edge = position.get('edge', 0)

        if edge > 0:
            win_prob = our_conf
        else:
            win_prob = our_conf * (1 - abs(edge))

        return random.random() < win_prob


# ============================================================================
# Unified Trader
# ============================================================================

class UnifiedTrader:
    """Single unified trader — bets UP or DOWN based on prediction."""

    def __init__(self, initial_balance: float = 100.0, interval: int = 30):
        self.interval = interval
        self.ws = WebSocketClient(symbol="BTC-USDT")
        self.strategy = UnifiedStrategyEngine()
        self.poly = PolymarketEdgeFinder()
        self.running = False
        self.cycle_count = 0

        self.wallet = Wallet(initial_balance)
        self.sizer = KellySizer(max_fraction=0.15, min_edge=0.01, half_kelly=True)
        self.resolver = MarketResolver()
        self.risk = RiskManager(
            max_drawdown_pct=0.30,
            max_consecutive_losses=6,
            pause_cycles=10,
            max_daily_loss_pct=0.15,
            max_position_pct=0.20,
        )

        self._init_db()
        self.strategy.attach_websocket(self.ws)
        self.ws.start()
        logger.info("WebSocket started, waiting for initial data...")
        import time as _time
        for i in range(15):
            s = self.ws.get_status()
            if s.get('mid_price', 0) > 0:
                logger.info(f"WebSocket ready: ${s['mid_price']:,.2f}")
                break
            _time.sleep(1)

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

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
                btc_entry_price REAL,
                btc_settlement_price REAL,
                resolution_source TEXT,
                resolution_price REAL,
                resolution_time TEXT,
                pnl REAL,
                win INTEGER,
                wallet_balance_after REAL,
                total_value_after REAL,
                cycle INTEGER,
                strategy TEXT
            )
        ''')
        # Add columns if table existed before migration
        for col, typ in [
            ("btc_entry_price", "REAL"),
            ("btc_settlement_price", "REAL"),
            ("resolution_source", "TEXT"),
            ("market_up_price", "REAL"),
            ("market_down_price", "REAL"),
        ]:
            try:
                conn.execute(f"ALTER TABLE sim_trades ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # Column already exists

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

    def _log_trade(self, trade: dict, kelly_frac: float) -> int:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute('''
            INSERT INTO sim_trades
            (timestamp, direction, market_slug, market_question,
             entry_price, amount, fee, shares, edge, our_confidence,
             kelly_fraction, status, btc_entry_price, btc_settlement_price,
             resolution_source, resolution_price, resolution_time,
             market_up_price, market_down_price,
             pnl, win, wallet_balance_after, total_value_after, cycle, strategy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            trade.get('btc_entry_price', 0),
            trade.get('btc_settlement_price'),
            trade.get('resolution_source'),
            trade.get('resolution_price'),
            trade.get('resolution_time'),
            trade.get('market_up_price', 0),
            trade.get('market_down_price', 0),
            trade.get('pnl', 0),
            1 if trade.get('win') else 0,
            self.wallet.balance,
            self.wallet.total_value,
            self.cycle_count,
            'UNIFIED',
        ))
        db_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return db_id

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
            'UNIFIED',
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

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def _check_pending_resolutions(self):
        now = time.time()
        resolved = []

        for pos in list(self.wallet.positions):
            try:
                ts = int(pos['market_slug'].split('-')[-1])
                if now > ts + 30:
                    resolved.append(pos)
            except:
                pass

        for pos in resolved:
            self._resolve_trade(pos)

    def _resolve_trade(self, position: dict):
        won, settlement_price, source = self.resolver.resolve_trade(position)
        position['btc_settlement_price'] = settlement_price
        position['resolution_source'] = source
        self.wallet.resolve_position(position, won)
        self.risk.record_trade(won, position.get('pnl', 0))
        self._update_trade_resolution(position)

        dir_str = "✅ WIN" if won else "❌ LOSS"
        pnl = position.get('pnl', 0)
        pnl_str = f"+${pnl:.4f}" if pnl > 0 else f"-${abs(pnl):.4f}"
        btc_entry = position.get('btc_entry_price', 0)
        
        # Show window prices if available
        slug = position['market_slug']
        wdata = self.resolver.window_data.get(slug, {})
        if wdata:
            wopen = wdata.get('open', 0)
            wclose = wdata.get('close', 0)
            arrow = "≥" if wclose >= wopen else "<"
            result = "UP" if wclose >= wopen else "DOWN"
            print(f"\n  📊 Window: open=${wopen:,.1f} → close=${wclose:,.1f} ({arrow}) → {result}")

        print(f"\n  {dir_str} #{position['id']}: {position['direction']} "
              f"${position['amount']:.2f} @ ${position['price']:.3f} | {pnl_str}")
        summary = self.wallet.get_summary()
        print(f"  Balance: ${summary['current_balance']:.2f} | "
              f"Return: {summary['total_return_pct']:.2f}% | "
              f"Win Rate: {summary['win_rate']:.1f}% | "
              f"Consec. Losses: {self.risk.consecutive_losses}")

    def _update_trade_resolution(self, position: dict):
        conn = sqlite3.connect(DB_PATH)
        conn.execute('''
            UPDATE sim_trades
            SET status=?, btc_settlement_price=?, resolution_source=?,
                resolution_price=?, resolution_time=?,
                pnl=?, win=?, wallet_balance_after=?, total_value_after=?
            WHERE id=? AND strategy=?
        ''', (
            position['status'],
            position.get('btc_settlement_price'),
            position.get('resolution_source'),
            position.get('resolution_price'),
            position.get('resolution_time'),
            position.get('pnl', 0),
            1 if position.get('win') else 0,
            self.wallet.balance,
            self.wallet.total_value,
            position['id'],
            'UNIFIED',
        ))
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # Trading Cycle
    # ------------------------------------------------------------------

    def run_cycle(self):
        """Run a single trading cycle."""
        self.cycle_count += 1

        # Check pending resolutions
        self._check_pending_resolutions()

        # Log portfolio
        self._log_portfolio()

        # Risk check
        allowed, reason = self.risk.check(self.wallet)
        if not allowed:
            if self.cycle_count % 10 == 0:
                print(f"\n⛔ {reason}")
            return

        # Get prediction
        ws_status = self.ws.get_status()
        prediction = self.strategy.predict()
        btc_price = ws_status.get('mid_price', 0)

        pred_direction = prediction.get('direction', 0)
        our_conf = prediction.get('confidence', 0.5)

        # Skip if NEUTRAL (direction == 0)
        if pred_direction == 0:
            return

        # Get Polymarket market
        market = self.poly.get_current_market()
        if not market:
            return

        # Determine bet direction and market price
        if pred_direction == 1:
            bet_direction = 'Up'
            market_price = market.get('up_price', 0.5)
        else:
            bet_direction = 'Down'
            market_price = market.get('down_price', 0.5)

        # Edge calculation
        edge = our_conf - market_price

        # --- Threshold checks ---
        # Primary filter: edge must exceed threshold
        conf_threshold = 0.55
        edge_threshold = 0.03

        if edge < edge_threshold or our_conf < conf_threshold:
            return

        # --- Kelly position sizing ---
        kelly_frac = self.sizer.calculate(edge, our_conf, market_price)
        if kelly_frac <= 0:
            return

        # Volatility-adjusted position size cap
        ws_status = self.ws.get_status()
        recent_vol = ws_status.get('volatility', 0.01)  # rough estimate
        vol_multiplier = max(0.3, min(1.0, 0.01 / max(recent_vol, 0.001)))
        adjusted_kelly = kelly_frac * vol_multiplier

        bet_amount = min(
            self.wallet.balance * adjusted_kelly,
            self.wallet.balance * self.risk.max_position_pct,
            1000.0,
        )
        bet_amount = max(bet_amount, self.risk.min_position)

        # Don't bet more than available
        if bet_amount > self.wallet.balance:
            bet_amount = self.wallet.balance * 0.5

        if bet_amount < self.risk.min_position:
            return

        # Feedback: record prediction direction for strategy adaptation
        self.strategy._last_direction = pred_direction
        self.strategy._last_entry_price = btc_price

        # Place bet
        trade = self.wallet.place_bet(
            direction=bet_direction,
            price=market_price,
            amount=bet_amount,
            market_slug=market.get('slug', ''),
            market_question=market.get('question', ''),
            edge=edge,
            our_confidence=our_conf,
            kelly_fraction=adjusted_kelly,
            btc_price=btc_price,
        )

        # Store Polymarket odds for dashboard
        trade['market_up_price'] = market.get('up_price', 0)
        trade['market_down_price'] = market.get('down_price', 0)

        # Record entry BTC price
        self.resolver.set_entry_price(market.get('slug', ''), btc_price)
        db_id = self._log_trade(trade, adjusted_kelly)
        trade['id'] = db_id

        # Print trade info
        print(f"\n🎯 Trade #{db_id}: "
              f"{'📈' if bet_direction == 'Up' else '📉'} {bet_direction} "
              f"${bet_amount:.2f} @ ${market_price:.3f} | "
              f"Edge: {edge*100:.2f}% | "
              f"Kelly: {adjusted_kelly*100:.1f}%")

    # ------------------------------------------------------------------
    # Main Loop
    # ------------------------------------------------------------------

    def run(self):
        """Main trading loop."""
        self.running = True

        print(f"\n{'='*60}")
        print(f"  🔮 Unified BTC Trader (Optimized)")
        print(f"  Balance: ${self.wallet.initial_balance:.2f}")
        print(f"  Interval: {self.interval}s")
        print(f"  Risk: {self.risk.max_drawdown_pct*100:.0f}% max DD, "
              f"{self.risk.max_consecutive_losses} max consec. losses")
        print(f"{'='*60}")

        try:
            while self.running:
                try:
                    self.run_cycle()
                except Exception as e:
                    logger.error(f"Cycle {self.cycle_count} error: {e}", exc_info=True)
                    print(f"\n❌ Cycle {self.cycle_count} error: {e}")
                time.sleep(self.interval)
        except KeyboardInterrupt:
            print("\n\n🛑 Stopping...")
        finally:
            self._print_summary()

    def run_once(self):
        """Single cycle for testing."""
        print(f"\n{'='*60}")
        print(f"  🔮 Single Trade Cycle")
        print(f"  Balance: ${self.wallet.initial_balance:.2f}")
        print(f"{'='*60}")
        try:
            self.run_cycle()
        except Exception as e:
            logger.error(f"Run once error: {e}", exc_info=True)
            print(f"\n❌ Error: {e}")
        self._print_summary()

    def _print_summary(self):
        summary = self.wallet.get_summary()
        print(f"\n{'='*60}")
        print(f"  📊 Session Summary")
        print(f"{'='*60}")
        print(f"  Trades: {summary['total_trades']} "
              f"(W:{summary['wins']} L:{summary['losses']})")
        print(f"  Win Rate: {summary['win_rate']:.1f}%")
        print(f"  P&L: ${summary['total_pnl']:.2f} "
              f"({summary['total_return_pct']:.2f}%)")
        print(f"  Max DD: {summary['max_drawdown_pct']:.2f}%")
        print(f"  Balance: ${summary['current_balance']:.2f}")
        print(f"  Fees: ${summary['total_fees']:.4f}")
        print(f"{'='*60}")


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Unified BTC Polymarket Trader")
    parser.add_argument("--once", action="store_true", help="Single cycle")
    parser.add_argument("--interval", type=int, default=30, help="Cycle interval (seconds)")
    parser.add_argument("--balance", type=float, default=100.0, help="Initial balance (USD)")
    args = parser.parse_args()

    trader = UnifiedTrader(initial_balance=args.balance, interval=args.interval)

    if args.once:
        trader.run_once()
    else:
        trader.run()
