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
                cycle INTEGER
            )
        ''')
        # Migrate: add missing columns
        existing = [r[1] for r in conn.execute("PRAGMA table_info(real_trades)").fetchall()]
        if 'resolution_source' not in existing:
            conn.execute("ALTER TABLE real_trades ADD COLUMN resolution_source TEXT")
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
            funder=self.proxy_wallet,  # Required: deposit wallet address
            signature_type=3,  # POLY_1271 proxy wallet
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
            "SELECT * FROM real_trades WHERE pnl IS NULL AND status IN ('matched','live','filled')"
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

            # Calculate PnL
            price = trade.get('price', 0)
            size = fill['filled_size']
            if not size or size <= 0:
                logger.debug(f"Skipping #{trade['id']}: not filled (size={size})")
                continue  # Skip unfilled trades
            if price > 0 and size > 0:
                pnl = size * (1.0 - price) if won else -size * price
            else:
                pnl = 0

            # Update DB
            c = sqlite3.connect(DB_PATH)
            c.execute(
                """UPDATE real_trades
                   SET status='settled', pnl=?, filled_size=?, filled_price=?,
                       resolution_source=?
                   WHERE id=?""",
                (round(pnl, 6), fill['filled_size'], fill['filled_price'], source, trade['id'])
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
        
        # Decision
        if edge and edge.get('action', '').startswith('BUY'):
            # Kelly position sizing
            from sim_trader import KellySizer
            sizer = KellySizer(max_fraction=0.10, min_edge=0.01)
            kelly_frac = sizer.calculate(
                float(edge['edge']),
                float(edge['our_confidence']),
                float(edge['market_price'])
            )
            
            if kelly_frac > 0:
                # Get balance to calculate bet size
                try:
                    from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
                    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                    balance_info = self.client.get_balance_allowance(params) if self.client else {}
                    available = float(balance_info.get("balance", 0)) / 1e6 if isinstance(balance_info, dict) else 100.0  # USDC 6-dec
                    bet_amount = min(
                        available * kelly_frac,
                        available * 0.15,  # Max 15% per trade
                        1000.0  # Max $1000 per trade
                    )
                    bet_amount = max(bet_amount, 5.0)  # Min $5 (Polymarket minimum)
                    
                    print(f"\n{'='*70}")
                    print(f"🔮 Real Trade Cycle #{self.cycle_count} | {datetime.now(timezone.utc).isoformat()}")
                    print(f"{'='*70}")
                    print(f"  💰 BTC: ${ws_status.get('mid_price', 0):,.2f}")
                    print(f"  🎯 Prediction: {'UP' if prediction['direction'] == 1 else 'DN'} ({prediction['confidence']:.1%})")
                    print(f"  📊 Edge: {edge['edge_pct']} | Action: {edge['action']}")
                    print(f"  📈 Market: Up ${edge['market_up_price']:.3f} / Down ${edge['market_down_price']:.3f}")
                    print(f"  💼 Kelly: {kelly_frac:.1%} | Bet: ${bet_amount:.2f}")
                    
                    # Place order
                    self.place_order(
                        direction=edge['bet_on'],
                        price=edge['market_price'],
                        amount_usdt=bet_amount,
                        market=market,
                        edge=edge,
                    )
                    
                except Exception as e:
                    logger.error(f"Trading cycle error: {e}", exc_info=True)
        else:
            print(f"\n{'='*70}")
            print(f"🔮 Cycle #{self.cycle_count} | {datetime.now(timezone.utc).isoformat()}")
            print(f"{'='*70}")
            print(f"  💰 BTC: ${ws_status.get('mid_price', 0):,.2f}")
            print(f"  🎯 Prediction: {'UP' if prediction['direction'] == 1 else 'DN'} ({prediction['confidence']:.1%})")
            if edge:
                print(f"  📊 Edge: {edge['edge_pct']} | Action: {edge['action']}")
            print(f"  ⏸ HOLD — No edge ≥2% or confidence < 52%")

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
        
        self.running = True
        print(f"\n{'='*70}")
        print(f"🚀 REAL TRADING STARTED")
        print(f"   Wallet: {self.deposit_wallet[:10]}...")
        print(f"   Chain: Polygon (137)")
        print(f"   Min Edge: 1% | Min Confidence: 50.5%")
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
