"""Check USDC balance and deposit into CLOB"""
from web3 import Web3
import json

wallet = "0x27c66a42DDb2EC9f1db2361447d551371cC06bac"
private_key = "0x75d434252c68a3a4272beda3d8bd8e279d4c4b9c51fb6c7066ee083374fbca0a"

# Native USDC on Polygon (Circle)
native_usdc = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
# Bridged USDC on Polygon
bridged_usdc = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# Collateral contract
collateral_addr = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
# Exchange V2
exchange_v2 = "0xE111180000d2663C0091e4f400237545B87B996B"

# USDC ABI
usdc_abi = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
]

# Collateral ABI
collateral_abi = [
    {"constant": False, "inputs": [{"name": "_collateral", "type": "address"}, {"name": "_amount", "type": "uint256"}], "name": "deposit", "outputs": [], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
]

# Try multiple RPCs
rpcs = [
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-mainnet.public.blastapi.io",
]

w3 = None
for url in rpcs:
    try:
        w3 = Web3(Web3.HTTPProvider(url))
        if w3.is_connected():
            print(f"Connected to: {url}")
            break
    except Exception as e:
        print(f"Failed {url}: {e}")
        continue

if not w3 or not w3.is_connected():
    print("Failed to connect to any RPC")
    exit(1)

usdc_contract = w3.eth.contract(address=native_usdc, abi=usdc_abi)
bridged_contract = w3.eth.contract(address=bridged_usdc, abi=usdc_abi)
collateral_contract = w3.eth.contract(address=collateral_addr, abi=collateral_abi)

# Check balances
print(f"\nWallet: {wallet}")

try:
    native_balance = usdc_contract.functions.balanceOf(wallet).call()
    print(f"Native USDC: ${native_balance / 1e6:.2f}")
except Exception as e:
    print(f"Native USDC check failed: {e}")

try:
    bridged_balance = bridged_contract.functions.balanceOf(wallet).call()
    print(f"Bridged USDC: ${bridged_balance / 1e6:.2f}")
except Exception as e:
    print(f"Bridged USDC check failed: {e}")

try:
    collat_balance = collateral_contract.functions.balanceOf(wallet).call()
    print(f"Collateral: ${collat_balance / 1e6:.2f}")
except Exception as e:
    print(f"Collateral check failed: {e}")

# Determine which USDC to use
total_usdc = 0
usdc_address = None
try:
    if native_balance > 0:
        total_usdc = native_balance
        usdc_address = native_usdc
        print(f"\nUsing Native USDC: ${total_usdc / 1e6:.2f}")
    elif bridged_balance > 0:
        total_usdc = bridged_balance
        usdc_address = bridged_usdc
        print(f"\nUsing Bridged USDC: ${total_usdc / 1e6:.2f}")
except:
    pass

if total_usdc > 0:
    amount = total_usdc
    print(f"\n=== Depositing ${amount / 1e6:.2f} into CLOB ===")
    
    # Step 1: Approve USDC spending by collateral contract
    print(f"\nStep 1: Approving USDC spending...")
    nonce = w3.eth.get_transaction_count(wallet)
    gas_price = w3.eth.gas_price
    
    approve_tx = usdc_contract.functions.approve(collateral_addr, amount).build_transaction({
        'from': wallet,
        'nonce': nonce,
        'gas': 100000,
        'gasPrice': gas_price,
        'chainId': 137,
    })
    
    signed = w3.eth.account.sign_transaction(approve_tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"Approve TX: {tx_hash.hex()}")
    
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    print(f"Approve status: {'Success' if receipt.status == 1 else 'Failed'}")
    
    # Step 2: Deposit USDC into collateral contract
    print(f"\nStep 2: Depositing USDC into collateral...")
    nonce = w3.eth.get_transaction_count(wallet)
    
    deposit_tx = collateral_contract.functions.deposit(usdc_address, amount).build_transaction({
        'from': wallet,
        'nonce': nonce,
        'gas': 200000,
        'gasPrice': gas_price,
        'chainId': 137,
    })
    
    signed = w3.eth.account.sign_transaction(deposit_tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"Deposit TX: {tx_hash.hex()}")
    
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    print(f"Deposit status: {'Success' if receipt.status == 1 else 'Failed'}")
    
    # Check collateral balance after deposit
    collat_balance = collateral_contract.functions.balanceOf(wallet).call()
    print(f"\nCollateral Balance after deposit: ${collat_balance / 1e6:.2f}")
    
    # Step 3: Update CLOB balance allowance
    print(f"\nStep 3: Updating CLOB balance allowance...")
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
    
    print(f"\nDeposit complete!")
else:
    print(f"\nNo USDC found. Please transfer USDC to {wallet}")
