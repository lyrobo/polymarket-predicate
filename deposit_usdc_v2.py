"""Deposit USDC into Polymarket CLOB - Version 2"""
from web3 import Web3
import json

wallet = "0x27c66a42DDb2EC9f1db2361447d551371cC06bac"
private_key = "0x75d434252c68a3a4272beda3d8bd8e279d4c4b9c51fb6c7066ee083374fbca0a"

native_usdc = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
collateral_addr = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
exchange_v2 = "0xE111180000d2663C0091e4f400237545B87B996B"

w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))

# USDC ABI
usdc_abi = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]

# Collateral ABI (full)
collateral_abi = [
    {"inputs": [{"name": "_collateral", "type": "address"}, {"name": "_amount", "type": "uint256"}], "name": "deposit", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "name", "outputs": [{"name": "", "type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "stateMutability": "view", "type": "function"},
]

usdc_contract = w3.eth.contract(address=native_usdc, abi=usdc_abi)
collateral_contract = w3.eth.contract(address=collateral_addr, abi=collateral_abi)

# Check allowance
allowance = usdc_contract.functions.allowance(wallet, collateral_addr).call()
print(f"USDC Allowance to collateral: ${allowance / 1e6:.2f}")

# Check collateral balance
collat_balance = collateral_contract.functions.balanceOf(wallet).call()
print(f"Collateral balance: ${collat_balance / 1e6:.2f}")

# Check collateral info
try:
    decimals = collateral_contract.functions.decimals().call()
    print(f"Collateral decimals: {decimals}")
except:
    print("Collateral decimals: N/A")

try:
    name = collateral_contract.functions.name().call()
    print(f"Collateral name: {name}")
except:
    print("Collateral name: N/A")

try:
    symbol = collateral_contract.functions.symbol().call()
    print(f"Collateral symbol: {symbol}")
except:
    print("Collateral symbol: N/A")

# Try deposit
amount = 200 * 10**6  # 200 USDC
nonce = w3.eth.get_transaction_count(wallet)
gas_price = w3.eth.gas_price

print(f"\n=== Attempting deposit ===")
print(f"Amount: ${amount / 1e6:.2f}")
print(f"Gas price: {gas_price}")

deposit_tx = collateral_contract.functions.deposit(native_usdc, amount).build_transaction({
    'from': wallet,
    'nonce': nonce,
    'gas': 300000,
    'gasPrice': gas_price,
    'chainId': 137,
})

# Try to simulate the transaction first
try:
    result = w3.eth.call(deposit_tx)
    print(f"Simulation result: {result.hex()}")
except Exception as e:
    print(f"Simulation failed: {e}")

# Sign and send
signed = w3.eth.account.sign_transaction(deposit_tx, private_key)
tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
print(f"Deposit TX: {tx_hash.hex()}")

receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
print(f"Deposit status: {'Success' if receipt.status == 1 else 'Failed'}")
print(f"Gas used: {receipt.gasUsed}")

# Check logs
if receipt.logs:
    print(f"Logs: {len(receipt.logs)}")
    for log in receipt.logs:
        print(f"  Address: {log.address}")
        print(f"  Topics: {[t.hex() for t in log.topics]}")
        print(f"  Data: {log.data.hex()}")

# Check collateral balance after deposit
collat_balance = collateral_contract.functions.balanceOf(wallet).call()
print(f"Collateral balance after: ${collat_balance / 1e6:.2f}")

# Update CLOB balance allowance
print(f"\n=== Updating CLOB balance allowance ===")
from py_clob_client_v2 import ClobClient, ApiCreds
from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=private_key,
    creds=ApiCreds(
        api_key="c742e0d7-31d1-931b-f6d1-80e56d649db8",
        api_secret="nlBJU23DkTPPwOQCCVYoh1opHl_f6q7MOqK9dCt_gA0=",
        api_passphrase="c5687b041c27e33719867aa3ff875f86d742d1fe275f5bae67227943f30953cb",
    ),
)

params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
result = client.update_balance_allowance(params)
print(f"Update result: {result}")

balance = client.get_balance_allowance(params)
print(f"CLOB Balance: {balance}")
