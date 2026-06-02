"""
Real Polymarket Trader - Connect wallet and place real orders
=============================================================
Integrates with py-clob-client-v2 SDK to place real trades on Polymarket.

Setup required:
  1. Create a Polygon wallet (private key)
  2. Fund wallet with USDC on Polygon
  3. Derive API credentials (one-time)
  4. Create deposit wallet (one-time)
  5. Transfer USDC to deposit wallet
  6. Start trading

Usage:
    python3 real_trader.py --setup          # Initial setup (create credentials)
    python3 real_trader.py --once           # Single trade attempt
    python3 real_trader.py --interval N     # Continuous trading
    python3 real_trader.py --status         # Check wallet status
    python3 real_trader.py --balance        # Check balance

Environment variables:
    POLY_PRIVATE_KEY    - Wallet private key (0x...)
    POLY_API_KEY        - L2 API key (after setup)
    POLY_API_SECRET     - L2 API secret
    POLY_API_PASSPHRASE - L2 API passphrase
    POLY_DEPOSIT_WALLET - Deposit wallet address (after setup)
"""

import json
import os
import sys
import time
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional
import urllib.request
import urllib.parse

# Workaround for Python 3.14 SSL handshake timeout with Polymarket
import httpx

# Patch the py_clob_client_v2 HTTP client BEFORE any other module imports it
def _patch_clob_http():
    import py_clob_client_v2.http_helpers.helpers as h
    import py_clob_client_v2.http_helpers.helpers
    h._http_client = httpx.Client(http2=True, verify=False)
    # Also patch any cached references
    py_clob_client_v2.http_helpers.helpers._http_client = httpx.Client(http2=True, verify=False)

_patch_clob_http()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from websocket_collector import WebSocketClient
from unified_strategy import UnifiedStrategyEngine
from realtime_service import PolymarketEdgeFinder
from window_reversion import WindowReversionDetector
from cross_market_arb import CrossMarketArbEngine

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
DB_PATH = os.path.join(DATA_DIR, 'btc_predictor.db')

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'real_trader.log')),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)

# Polymarket config
POLY_HOST = "https://clob.polymarket.com"
POLY_CHAIN_ID = 137  # Polygon mainnet
POLY_GAMMA = "https://gamma-api.polymarket.com"


class RealPolymarketTrader:
    """Real Polymarket trader with wallet integration."""

    def __init__(self):
        self.private_key = os.getenv("POLY_PRIVATE_KEY", "")
        self.api_key = os.getenv("POLY_API_KEY", "")
        self.api_secret = os.getenv("POLY_API_SECRET", "")
        self.api_passphrase = os.getenv("POLY_API_PASSPHRASE", "")
        self.deposit_wallet = os.getenv("POLY_DEPOSIT_WALLET", "")
        self.proxy_wallet = os.getenv("POLY_PROXY_WALLET", "")
        
        self.client = None
        self.ws = WebSocketClient(symbol="BTC-USDT")
        self.strategy = UnifiedStrategyEngine()
        self.poly = PolymarketEdgeFinder()
        self.window_reversion = WindowReversionDetector(
            drop_threshold=30.0,    # $30 drop → buy UP
            pump_threshold=30.0,    # $30 pump → buy DOWN
            min_odds=0.49,          # token still ≥49¢
            min_time_remaining=90,  # ≥90s left for reversion
            max_time_from_start=240, # don't trade in last 60s
            min_confidence=0.70,    # 70% confidence minimum
        )
        self.cross_market = CrossMarketArbEngine(
            entry_threshold=2.0,    # 2 sigma entry
            exit_threshold=0.3,     # 0.3 sigma exit
            min_orderbook_imbalance=0.15,  # |I_t| > 15%
            min_confidence=0.70,
        )
        self.running = False
        self.cycle_count = 0
        
        # Trade history
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(DB_PATH)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS real_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                order_id TEXT,
                direction TEXT,
                market_slug TEXT,
                market_question TEXT,
                token_id TEXT,
                price REAL,
                size REAL,
                side TEXT,
                edge REAL,
                our_confidence REAL,
                status TEXT,
                filled_size REAL,
                filled_price REAL,
                pnl REAL,
                resolution_source TEXT,
                balance_after REAL,
                cycle INTEGER
            )
        ''')
        # Migrate: add missing columns
        existing = [r[1] for r in conn.execute("PRAGMA table_info(real_trades)").fetchall()]
        if 'resolution_source' not in existing:
            conn.execute("ALTER TABLE real_trades ADD COLUMN resolution_source TEXT")
        if 'balance_after' not in existing:
            conn.execute("ALTER TABLE real_trades ADD COLUMN balance_after REAL")
        conn.commit()
        conn.close()

    def is_configured(self) -> bool:
        """Check if all credentials are set."""
        return all([
            self.private_key,
            self.api_key,
            self.api_secret,
            self.api_passphrase,
            self.deposit_wallet,
        ])

    def setup_client(self):
        """Initialize CLOB client with credentials."""
        from py_clob_client_v2 import ClobClient, ApiCreds
        
        if not self.is_configured():
            raise ValueError(
                "Missing credentials. Set environment variables:\n"
                "  POLY_PRIVATE_KEY\n"
                "  POLY_API_KEY\n"
                "  POLY_API_SECRET\n"
                "  POLY_API_PASSPHRASE\n"
                "  POLY_DEPOSIT_WALLET\n"
                "Or run --setup to create new credentials."
            )
        
        api_creds = ApiCreds(
            api_key=self.api_key,
            api_secret=self.api_secret,
            api_passphrase=self.api_passphrase,
        )
        
        self.client = ClobClient(
            host=POLY_HOST,
            chain_id=POLY_CHAIN_ID,
            key=self.private_key,
            creds=api_creds,
            funder=self.proxy_wallet or self.deposit_wallet,
            signature_type=3,  # POLY_1271 for proxy wallet
        )
        
        logger.info(f"CLOB client initialized: wallet={self.deposit_wallet[:10]}...")
        return self.client

    def setup_new_credentials(self):
        """Create new API credentials from private key (L1 -> L2)."""
        from py_clob_client_v2 import ClobClient
        
        if not self.private_key:
            raise ValueError("POLY_PRIVATE_KEY not set")
        
        # L1 client (no credentials yet)
        client = ClobClient(
            host=POLY_HOST,
            chain_id=POLY_CHAIN_ID,
            key=self.private_key,
        )
        
        logger.info("Deriving API credentials (L1 -> L2)...")
        creds = client.create_or_derive_api_key()
        
        print("\n" + "=" * 60)
        print("🔑 API Credentials Generated")
        print("=" * 60)
        print(f"  API Key:      {creds.api_key}")
        print(f"  API Secret:   {creds.api_secret}")
        print(f"  Passphrase:   {creds.api_passphrase}")
        print()
        print("Save these! Add to your .env file:")
        print(f"  export POLY_API_KEY={creds.api_key}")
        print(f"  export POLY_API_SECRET={creds.api_secret}")
        print(f"  export POLY_API_PASSPHRASE={creds.api_passphrase}")
        print("=" * 60)
        
        return creds

    def get_deposit_wallet_address(self) -> str:
        """Derive the deposit wallet address for the current signer."""
        from py_clob_client_v2 import ClobClient
        
        client = ClobClient(
            host=POLY_HOST,
            chain_id=POLY_CHAIN_ID,
            key=self.private_key,
        )
        
        # Get expected deposit wallet address
        addr = client.get_address()
        print(f"\n📬 Deposit Wallet Address: {addr}")
        print("   Transfer USDC to this address to fund trading.")
        return addr

    def check_status(self):
        """Check wallet status, balance, and credentials."""
        print("\n" + "=" * 60)
        print("📊 Polymarket Wallet Status")
        print("=" * 60)
        
        # Check credentials
        print(f"\n  Private Key:    {'✅ Set' if self.private_key else '❌ Not set'}")
        print(f"  API Key:        {'✅ Set' if self.api_key else '❌ Not set'}")
        print(f"  API Secret:     {'✅ Set' if self.api_secret else '❌ Not set'}")
        print(f"  Passphrase:     {'✅ Set' if self.api_passphrase else '❌ Not set'}")
        print(f"  Deposit Wallet: {'✅ ' + self.deposit_wallet[:10] + '...' if self.deposit_wallet else '❌ Not set'}")
        
        if not self.is_configured():
            print("\n  ⚠️  Not fully configured. Run --setup to create credentials.")
            return
        
        # Initialize client and check balance
        try:
            self.setup_client()
            
            # Get balance and allowance
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            balance = self.client.get_balance_allowance(params)
            print(f"\n  💰 Balance & Allowance:")
            print(f"    {json.dumps(balance, indent=4, default=str)}")
            
            # Get open orders
            orders = self.client.get_open_orders()
            print(f"\n  📋 Open Orders: {len(orders) if orders else 0}")
            for o in (orders or [])[:5]:
                print(f"    {o.get('id', 'N/A')[:20]}... | {o.get('side', '?')} | "
                      f"${o.get('price', 0):.3f} | {o.get('original_size', 0):.2f} shares")
            
            # Get recent trades
            trades = self.client.get_trades()
            print(f"\n  📈 Recent Trades: {len(trades) if trades else 0}")
            for t in (trades or [])[:5]:
                print(f"    {t.get('id', 'N/A')[:20]}... | {t.get('side', '?')} | "
                      f"${t.get('price', 0):.3f} | {t.get('size', 0):.4f} shares")
            
        except Exception as e:
            print(f"\n  ❌ Error: {e}")
            import traceback
            traceback.print_exc()

    def get_market_token(self, market: dict, side: str) -> Optional[str]:
        """Get the token ID for a specific side of a market."""
        token_ids = market.get('token_ids', [])
        if not token_ids:
            return None
        
        # token_ids[0] = Up/Yes, token_ids[1] = Down/No
        if side == "Up":
            return token_ids[0] if len(token_ids) > 0 else None
        else:
            return token_ids[1] if len(token_ids) > 1 else None

    def place_order(self, direction: str, price: float, amount_usdt: float,
                    market: dict, edge: dict) -> Optional[dict]:
        """Place a real order on Polymarket.
        
        Args:
            direction: 'Up' or 'Down'
            price: Market price
            amount_usdt: Amount in USDT to spend
            market: Market data from Polymarket
            edge: Edge analysis
        """
        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client_v2.order_builder.constants import BUY
        
        # Get token ID
        token_id = self.get_market_token(market, direction)
        if not token_id:
            logger.error(f"Could not get token ID for {direction}")
            return None
        
        # Ensure numeric types
        price = float(price)
        amount_usdt = float(amount_usdt)
        
        # Calculate size (shares)
        size = amount_usdt / price if price > 0 else 0
        
        # Round to tick size (0.01 for most markets)
        tick_size = 0.01
        price = round(price / tick_size) * tick_size
        size = round(size, 2)
        
        if size < 1:
            logger.warning(f"Size too small: {size:.2f} shares (min 1)")
            return None
        
        print(f"\n  🎲 PLACING REAL ORDER:")
        print(f"     Market: {market.get('question', '')[:50]}")
        print(f"     Direction: {direction}")
        print(f"     Token: {token_id[:20]}...")
        print(f"     Price: ${price:.3f}")
        print(f"     Size: {size:.2f} shares")
        print(f"     Amount: ${amount_usdt:.2f} USDT")
        print(f"     Edge: {edge.get('edge_pct', 'N/A')}")
        
        try:
            # Place order using SDK
            response = self.client.create_and_post_order(
                OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=BUY,
                ),
                options=PartialCreateOrderOptions(
                    tick_size="0.01",
                    neg_risk=False,
                ),
                order_type=OrderType.GTC,
            )
            
            order_id = response.get("orderID", "unknown")
            status = response.get("status", "unknown")
            
            print(f"\n  ✅ Order placed!")
            print(f"     Order ID: {order_id[:30]}...")
            print(f"     Status: {status}")
            
            # Log to database
            self._log_real_trade({
                'order_id': order_id,
                'direction': direction,
                'market_slug': market.get('slug', ''),
                'market_question': market.get('question', ''),
                'token_id': token_id,
                'price': price,
                'size': size,
                'side': 'BUY',
                'edge': edge.get('edge', 0),
                'our_confidence': edge.get('our_confidence', 0.5),
                'status': status,
            })
            
            return response
            
        except Exception as e:
            print(f"\n  ❌ Order failed: {e}")
            logger.error(f"Order placement error: {e}", exc_info=True)
            return None

    def _log_real_trade(self, trade: dict):
        conn = sqlite3.connect(DB_PATH)
        conn.execute('''
            INSERT INTO real_trades
            (timestamp, order_id, direction, market_slug, market_question,
             token_id, price, size, side, edge, our_confidence, status, cycle)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now(timezone.utc).isoformat(),
            trade.get('order_id', ''),
            trade.get('direction', ''),
            trade.get('market_slug', ''),
            trade.get('market_question', ''),
            trade.get('token_id', ''),
            trade.get('price', 0),
            trade.get('size', 0),
            trade.get('side', ''),
            trade.get('edge', 0),
            trade.get('our_confidence', 0.5),
            trade.get('status', ''),
            self.cycle_count,
        ))
        conn.commit()
        conn.close()

    def _get_balance(self) -> float:
        """Fetch current USDC balance from Polymarket CLOB."""
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            if self.client:
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                info = self.client.get_balance_allowance(params)
                if isinstance(info, dict):
                    return float(info.get('balance', 0)) / 1e6
        except Exception as e:
            logger.debug(f"Balance fetch failed: {e}")
        return 0.0

    def _load_risk_state(self) -> dict:
        """Load persisted risk state (peak balance)."""
        import json
        state_path = os.path.join(DATA_DIR, 'risk_state.json')
        try:
            with open(state_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_risk_state(self, state: dict):
        """Persist risk state."""
        import json
        state_path = os.path.join(DATA_DIR, 'risk_state.json')
        with open(state_path, 'w') as f:
            json.dump(state, f, default=str)

    def _get_daily_pnl(self) -> float:
        """Sum today's settled PnL (UTC calendar day)."""
        conn = sqlite3.connect(DB_PATH)
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM real_trades "
            "WHERE status='settled' AND substr(timestamp,1,10)=?",
            (today,)
        ).fetchone()
        conn.close()
        return row[0] if row else 0.0

    def _get_consecutive_losses(self) -> int:
        """Count consecutive losing trades (most recent settled first)."""
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT pnl FROM real_trades WHERE status='settled' ORDER BY id DESC LIMIT 30"
        ).fetchall()
        conn.close()
        count = 0
        for r in rows:
            if r[0] is not None and r[0] < 0:
                count += 1
            else:
                break  # stop at first non-loss
        return count

    def _get_total_exposure(self) -> float:
        """Sum open position value (status in matched/live/filled)."""
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT COALESCE(SUM(price * COALESCE(filled_size, size)), 0) "
            "FROM real_trades WHERE status IN ('matched','live','filled')"
        ).fetchone()
        conn.close()
        return row[0] if row else 0.0

    def _check_risk_limits(self, available: float) -> tuple[bool, str]:
        """Check all risk limits. Returns (allowed, reason).

        Checks:
          1. Daily loss ≤ $50
          2. Consecutive losses < 5
          3. Max drawdown ≤ 30%
          4. Total exposure ≤ 50% of balance
          5. Balance ≥ $5 minimum
        """
        # 1. Daily loss limit
        daily_pnl = self._get_daily_pnl()
        if daily_pnl <= -50.0:
            return False, f"🚨 Daily loss limit hit: ${daily_pnl:.2f} (max -$50)"

        # 2. Consecutive losses
        lose_streak = self._get_consecutive_losses()
        if lose_streak >= 5:
            return False, f"🚨 {lose_streak} consecutive losses — pausing"

        # 3. Max drawdown (30%)
        state = self._load_risk_state()
        peak = state.get('peak_balance', available)
        if available > peak:
            peak = available
            state['peak_balance'] = peak
            self._save_risk_state(state)
        drawdown = (peak - available) / peak if peak > 0 else 0
        if drawdown >= 0.30:
            return False, f"🚨 Max drawdown {drawdown:.1%} ≥ 30% (peak=${peak:.2f}, now=${available:.2f})"

        # 4. Total exposure
        exposure = self._get_total_exposure()
        if available > 0 and (exposure / available) > 0.50:
            return False, f"🚨 Exposure {exposure/available:.1%} ≥ 50% (open=${exposure:.2f}, bal=${available:.2f})"

        # 5. Balance floor
        if available < 5.0:
            return False, f"🚨 Balance ${available:.2f} < $5 minimum"

        return True, "✅ All risk checks passed"

    def _check_order_fill(self, order_id: str) -> dict:
        """Check order fill status from CLOB API."""
        try:
            url = f"{POLY_HOST}/data/order/{order_id}"
            req = urllib.request.Request(url, headers={"User-Agent": "BTC-Predictor/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                return {
                    'status': data.get('status', 'unknown'),
                    'filled_size': float(data.get('size_matched', 0) or 0),
                    'filled_price': float(data.get('price', 0) or 0),
                }
        except Exception as e:
            logger.debug(f"Order status check failed for {order_id}: {e}")
            return {'status': 'unknown', 'filled_size': 0, 'filled_price': 0}

    def _check_market_outcome(self, market_slug: str) -> dict:
        """Check if market is resolved on Polymarket Gamma API.

        Returns dict with keys: resolved, closed, up_won, down_won.
        outcomes[0] = Up, outcomes[1] = Down; "1" = winner, "0" = loser.
        """
        try:
            url = f"{POLY_GAMMA}/events?slug={urllib.parse.quote(market_slug)}&limit=1"
            req = urllib.request.Request(url, headers={"User-Agent": "BTC-Predictor/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            if not data:
                return {'resolved': False, 'closed': False}

            event = data[0]
            closed = event.get('closed', False)
            markets = event.get('markets', [])
            if not markets:
                return {'resolved': False, 'closed': closed}

            m = markets[0]
            outcomes = m.get('outcomePrices', None)
            if outcomes and closed:
                up_won = str(outcomes[0]) == "1"
                down_won = str(outcomes[1]) == "1"
                return {
                    'resolved': True,
                    'closed': True,
                    'up_won': up_won,
                    'down_won': down_won,
                }

            return {'resolved': False, 'closed': closed}
        except Exception as e:
            logger.debug(f"Market outcome check failed for {market_slug}: {e}")
            return {'resolved': False, 'closed': False}

    def _okx_settle(self, trade: dict):
        """Fallback: determine win/loss using OKX K-line OHLC data.

        Returns True/False if resolved, None if unable to determine.
        """
        try:
            market_slug = trade['market_slug']
            ts = int(market_slug.split('-')[-1])
            window_end = datetime.fromtimestamp(ts, tz=timezone.utc)
            window_start = window_end - timedelta(minutes=5)

            start_ms = int(window_start.timestamp() * 1000)
            end_ms = int(window_end.timestamp() * 1000)

            url = (f"https://www.okx.com/api/v5/market/history-candles"
                   f"?instId=BTC-USDT&bar=1m&before={end_ms}&after={start_ms}&limit=10")
            req = urllib.request.Request(url, headers={"User-Agent": "BTC-Predictor/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            if data.get('code') != '0' or not data.get('data'):
                return None

            klines = data['data']
            if len(klines) < 2:
                return None

            # K-lines are reverse chronological
            start_close = float(klines[-1][4])
            end_close = float(klines[0][4])

            direction = trade['direction']
            if direction == 'Up':
                return end_close > start_close
            else:
                return end_close < start_close
        except Exception as e:
            logger.warning(f"OKX settlement failed for #{trade.get('id')}: {e}")
            return None

    def settle_trades(self):
        """Settle all pending real trades: check order fills, then market outcome.

        For each unsettled trade:
          1. Poll CLOB for filled_size / filled_price
          2. If market is closed on Polymarket, use Gamma API outcomes
          3. If not yet resolved on-chain, fall back to OKX K-line
          4. Compute PnL and update DB
        """
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM real_trades WHERE pnl IS NULL AND status IN ('matched','live')"
        ).fetchall()
        conn.close()

        if not rows:
            return

        settled = 0
        for row in rows:
            trade = dict(row)
            order_id = trade['order_id']
            market_slug = trade['market_slug']

            # Step 1: check order fill status
            fill = self._check_order_fill(order_id)

            # Step 2: check market outcome
            outcome = self._check_market_outcome(market_slug)

            if not outcome['closed'] and not outcome['resolved']:
                # Market still active — update fill status only
                if fill['filled_size'] > 0 and (trade.get('filled_size') or 0) != fill['filled_size']:
                    c = sqlite3.connect(DB_PATH)
                    c.execute(
                        "UPDATE real_trades SET filled_size=?, filled_price=?, status='filled' WHERE id=?",
                        (fill['filled_size'], fill['filled_price'], trade['id'])
                    )
                    c.commit()
                    c.close()
                    logger.info(f"Updated fill #{trade['id']}: {fill['filled_size']} shares @ {fill['filled_price']}")
                continue

            # Market is closed — determine win/loss
            direction = trade['direction']
            if outcome['resolved']:
                won = outcome['up_won'] if direction == 'Up' else outcome['down_won']
                source = 'polymarket'
            else:
                # Closed but not yet resolved on-chain — use OKX fallback
                won = self._okx_settle(trade)
                if won is None:
                    continue
                source = 'okx'

            # Calculate PnL — only for filled orders
            price = trade.get('price', 0)
            filled_sz = fill['filled_size']
            if filled_sz == 0:
                # Order never filled — mark cancelled, no PnL
                c = sqlite3.connect(DB_PATH)
                c.execute(
                    "UPDATE real_trades SET status='cancelled', pnl=0, filled_size=0, filled_price=0 WHERE id=?",
                    (trade['id'],)
                )
                c.commit()
                c.close()
                settled += 1
                logger.info(f"Cancelled #{trade['id']}: {trade['direction']} never filled")
                continue

            if price > 0 and filled_sz > 0:
                pnl = filled_sz * (1.0 - price) if won else -filled_sz * price
            else:
                pnl = 0

            # Fetch current balance after settlement
            balance_after = self._get_balance()

            # Update DB
            c = sqlite3.connect(DB_PATH)
            c.execute(
                """UPDATE real_trades
                   SET status='settled', pnl=?, filled_size=?, filled_price=?,
                       resolution_source=?, balance_after=?
                   WHERE id=?""",
                (round(pnl, 6), fill['filled_size'], fill['filled_price'],
                 source, balance_after, trade['id'])
            )
            c.commit()
            c.close()

            settled += 1
            dir_str = "WIN" if won else "LOSS"
            logger.info(f"Settled #{trade['id']}: {direction} → {dir_str} pnl=${pnl:.4f} [{source}]")

        if settled:
            logger.info(f"Settled {settled} trades this cycle")

    def run_cycle(self):
        """Run a single trading cycle."""
        self.cycle_count += 1
        start = time.time()
        
        # Get prediction
        ws_status = self.ws.get_status()
        prediction = self.strategy.predict()
        
        # Get Polymarket market
        market = self.poly.get_current_market()
        edge = None
        if market:
            edge = self.poly.compute_edge(prediction, market)
        
        # === SIGNAL #1: Cross-Market Arb (L2 imbalance + spread z-score) ===
        ca_signal = None
        if market:
            btc = ws_status.get('mid_price', 0)
            up_p = market.get('up_price', 0.5)
            dn_p = market.get('down_price', 0.5)
            ca_signal = self.cross_market.analyze(up_p, dn_p, btc)

        # === SIGNAL #2: Window Reversion (BTC deviation) ===
        wr_signal = None
        if market:
            wr_signal = self.window_reversion.check(market)
        
        trade_direction = None
        trade_confidence = 0.0
        trade_price = 0.0
        trade_edge = 0.0
        signal_source = "model"
        
        # Priority: cross-market arb > window reversion > model
        if ca_signal and ca_signal['action'] in ('BUY_UP', 'BUY_DN'):
            trade_direction = 'Up' if ca_signal['action'] == 'BUY_UP' else 'Down'
            trade_confidence = ca_signal['confidence']
            trade_price = up_p if trade_direction == 'Up' else dn_p
            trade_edge = 0.03
            signal_source = "cross_market_arb"
            logger.info(
                f"📊 CROSS-ARB #{self.cycle_count}: {ca_signal['action']} "
                f"conf={trade_confidence:.1%} z={ca_signal['zscore']:.2f} "
                f"I={ca_signal['imbalance']:.2%} | {ca_signal.get('reason','')}"
            )
        elif wr_signal and wr_signal['action'] in ('BUY_UP', 'BUY_DN'):
            trade_direction = 'Up' if wr_signal['action'] == 'BUY_UP' else 'Down'
            trade_confidence = wr_signal['confidence']
            trade_price = wr_signal['up_price'] if trade_direction == 'Up' else wr_signal['down_price']
            trade_edge = 0.03
            signal_source = "window_reversion"
            logger.info(
                f"🔥 WINDOW REVERSION #{self.cycle_count}: {wr_signal['action']} "
                f"conf={trade_confidence:.1%} dev=${wr_signal['deviation']:,.0f} "
                f"({wr_signal['deviation_pct']:.2f}%) rem={wr_signal['time_remaining']}s "
                f"| {wr_signal.get('reason','')}"
            )
        elif edge and edge.get('action', '').startswith('BUY'):
            # Fall back to model prediction
            trade_direction = edge['bet_on']
            trade_confidence = edge.get('our_confidence', 0.5)
            trade_price = edge['market_price']
            trade_edge = float(edge.get('edge', 0))
        
        # Decision
        if trade_direction:
            # Get balance to calculate bet size
            try:
                from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
                
                # Retry balance fetch on connection errors
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                available = 0.0
                for attempt in range(3):
                    try:
                        if self.client:
                            balance_info = self.client.get_balance_allowance(params)
                            if isinstance(balance_info, dict):
                                available = float(balance_info.get('balance', 0)) / 1e6
                        break
                    except Exception as e:
                        if attempt < 2:
                            logger.warning(f"Balance fetch attempt {attempt+1} failed: {e}, retrying...")
                            time.sleep(2)
                        else:
                            raise
                
                if available <= 0:
                    logger.warning(f"No balance available (${available:.2f}), skipping trade")
                    return

                # Track max/min balance
                state = self._load_risk_state()
                state['max_balance'] = max(state.get('max_balance', available), available)
                state['min_balance'] = min(state.get('min_balance', available), available)
                self._save_risk_state(state)

                # === Risk checks ===
                risk_ok, risk_reason = self._check_risk_limits(available)
                btc_price = ws_status.get('mid_price', 0)
                
                if signal_source == "window_reversion":
                    logger.info(
                        f"🔥 CYCLE #{self.cycle_count} | BTC=${btc_price:,.0f} | "
                        f"WR: {wr_signal['action']} conf={trade_confidence:.1%} "
                        f"dev=${wr_signal['deviation']:,.0f} rem={wr_signal['time_remaining']}s "
                        f"| Risk: {risk_reason}"
                    )
                else:
                    logger.info(
                        f"🎯 CYCLE #{self.cycle_count} | BTC=${btc_price:,.0f} | "
                        f"Pred: {'UP' if prediction['direction']==1 else 'DN'} "
                        f"conf={trade_confidence:.1%} edge={trade_edge:.2%} "
                        f"| Risk: {risk_reason}"
                    )
                
                if not risk_ok:
                    logger.warning(f"Risk check blocked trade: {risk_reason}")
                else:
                    # Kelly sizing for reversion signals
                    if signal_source == "window_reversion":
                        kelly_frac = min(0.10, (trade_confidence - 0.5) * 0.5)
                    else:
                        from sim_trader import KellySizer
                        sizer = KellySizer(max_fraction=0.10, min_edge=0.005)
                        kelly_frac = sizer.calculate(trade_edge, trade_confidence, trade_price)
                    
                    if kelly_frac > 0:
                        bet_amount = min(
                            available * kelly_frac,
                            available * 0.15,
                            1000.0
                        )
                        bet_amount = max(bet_amount, 5.0)

                        logger.info(
                            f"💰 BET #{self.cycle_count}: {trade_direction} "
                            f"${bet_amount:.2f} @ ${trade_price:.3f} "
                            f"[{signal_source}] bal=${available:.2f}"
                        )

                        # Place order
                        trade_edge_dict = {
                            'edge': trade_edge,
                            'edge_pct': f'{trade_edge:.2%}',
                            'bet_on': trade_direction,
                            'our_confidence': trade_confidence,
                            'market_price': trade_price,
                            'market_up_price': market.get('up_price', 0.5) if market else 0.5,
                            'market_down_price': market.get('down_price', 0.5) if market else 0.5,
                            'action': f'BUY_{trade_direction.upper()}'
                        }
                        self.place_order(
                            direction=trade_direction,
                            price=trade_price,
                            amount_usdt=bet_amount,
                            market=market,
                            edge=trade_edge_dict,
                        )
                
            except Exception as e:
                logger.error(f"Trading cycle error: {e}", exc_info=True)
        else:
            # HOLD
            btc = ws_status.get('mid_price', 0)
            ca_info = f" z={ca_signal['zscore']:.2f} I={ca_signal['imbalance']:.2%}" if ca_signal else ""
            logger.info(f"Cycle #{self.cycle_count} HOLD | BTC=${btc:,.0f}{ca_info}")

    def run_continuous(self):
        """Run continuous trading."""
        if not self.is_configured():
            print("❌ Not configured. Run --setup first.")
            print("\nRequired environment variables:")
            print("  export POLY_PRIVATE_KEY=0x...")
            print("  export POLY_API_KEY=***")
            print("  export POLY_API_SECRET=***")
            print("  export POLY_API_PASSPHRASE=...")
            print("  export POLY_DEPOSIT_WALLET=0x...")
            return
        
        self.setup_client()
        self.ws.start()
        time.sleep(15)

        # 🧠 Connect WebSocket data to strategy engine for real-time predictions
        self.strategy.attach_websocket(self.ws)
        self.window_reversion.attach_websocket(self.ws)
        self.cross_market.attach_websocket(self.ws)

        # Record starting balance for PnL tracking
        start_balance = self._get_balance()
        state = self._load_risk_state()
        state['starting_balance'] = start_balance
        state['max_balance'] = max(state.get('max_balance', start_balance), start_balance)
        state['min_balance'] = min(state.get('min_balance', start_balance), start_balance)
        if 'peak_balance' not in state:
            state['peak_balance'] = start_balance
        self._save_risk_state(state)
        logger.info(f"Starting balance: ${start_balance:.2f}")
        
        self.running = True
        print(f"\n{'='*70}")
        print(f"🚀 REAL TRADING STARTED")
        print(f"   Wallet: {self.deposit_wallet[:10]}...")
        print(f"   Chain: Polygon (137)")
        print(f"   Risk Limits: Max DD 30% | Consec. Losses 5 | Daily Loss $50")
        print(f"   Min Edge: 0.5% | Max Bet: 15%/$1000 | Kelly Factor: 0.5")
        print(f"{'='*70}")
        
        try:
            while self.running:
                try:
                    self.run_cycle()
                    self.settle_trades()
                except Exception as e:
                    logger.error(f"Cycle error: {e}", exc_info=True)
                
                for _ in range(300):  # 30s interval
                    if not self.running:
                        break
                    time.sleep(0.1)
        except KeyboardInterrupt:
            logger.info("Trading stopped")
        finally:
            self.ws.stop()


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Real Polymarket Trader')
    parser.add_argument('--setup', action='store_true', help='Create API credentials')
    parser.add_argument('--wallet', action='store_true', help='Show deposit wallet address')
    parser.add_argument('--status', action='store_true', help='Check wallet status')
    parser.add_argument('--once', action='store_true', help='Single trade cycle')
    parser.add_argument('--interval', type=int, default=0, help='Continuous interval (seconds)')
    args = parser.parse_args()
    
    trader = RealPolymarketTrader()
    
    if args.setup:
        trader.setup_new_credentials()
    elif args.wallet:
        trader.get_deposit_wallet_address()
    elif args.status:
        trader.check_status()
    elif args.once:
        if not trader.is_configured():
            print("❌ Not configured. Run --setup first.")
            print("\nRequired environment variables:")
            print("  export POLY_PRIVATE_KEY=0x...")
            print("  export POLY_API_KEY=***")
            print("  export POLY_API_SECRET=***")
            print("  export POLY_API_PASSPHRASE=...")
            print("  export POLY_DEPOSIT_WALLET=0x...")
            sys.exit(1)
        
        trader.setup_client()
        trader.ws.start()
        time.sleep(15)
        trader.run_cycle()
        trader.ws.stop()
    elif args.interval > 0:
        trader.run_continuous()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
