"""Test CLOB order placement with proxy funder"""
from py_clob_client_v2 import ClobClient, ApiCreds
from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType, OrderArgsV2
from py_clob_client_v2.order_builder.constants import BUY

PRIVATE_KEY = "0x75d434252c68a3a4272beda3d8bd8e279d4c4b9c51fb6c7066ee083374fbca0a"
API_KEY = "c742e0d7-31d1-931b-f6d1-80e56d649db8"
API_SECRET = "nlBJU23DkTPPwOQCCVYoh1opHl_f6q7MOqK9dCt_gA0="
API_PASSPHRASE = "c5687b041c27e33719867aa3ff875f86d742d1fe275f5bae67227943f30953cb"
PROXY_FUNDER = "0xaC447078c16184016C6eF8BE97a1FA963b26Ff46"

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=PRIVATE_KEY,
    creds=ApiCreds(
        api_key=API_KEY,
        api_secret=API_SECRET,
        api_passphrase=API_PASSPHRASE,
    ),
    funder=PROXY_FUNDER,
)

# Check balance
params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
balance = client.get_balance_allowance(params)
print(f"CLOB Balance: {balance}")

# Get markets
markets = client.get_sampling_simplified_markets()
print(f"Markets: {markets}")

# Get a specific market
if markets and len(markets) > 0:
    market_id = markets[0]
    print(f"\nGetting market: {market_id}")
    try:
        market = client.get_market(market_id)
        print(f"Market: {market}")
    except Exception as e:
        print(f"Market error: {e}")

# Try to get order books
try:
    books = client.get_order_books([])
    print(f"Order books: {books}")
except Exception as e:
    print(f"Order books error: {e}")

# Check open orders
orders = client.get_open_orders()
print(f"Open orders: {orders}")
