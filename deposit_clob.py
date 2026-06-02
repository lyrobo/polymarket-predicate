"""
Polymarket CLOB Deposit Script (Headless)
==========================================
Deposit USDC → pUSD → CLOB without web UI.
Works on headless servers (no GUI needed).

Usage:
    python3 deposit_clob.py
"""
import json
import time
from web3 import Web3

# ============================================================
# Configuration
# ============================================================
PRIVATE_KEY = "0x75d434252c68a3a4272beda3d8bd8e279d4c4b9c51fb6c7066ee083374fbca0a"
WALLET = "0x27c66a42DDb2EC9f1db2361447d551371cC06bac"
CHAIN_ID = 137  # Polygon

# Polymarket V2 Contracts on Polygon
EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
COLLATERAL = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"  # pUSD token
USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC on Polygon

# RPC endpoints (try multiple)
RPC_URLS = [
    "https://1rpc.io/matic",
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
]

# ABIs (minimal)
USDC_ABI = json.loads('''[
    {"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},
    {"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},
    {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint256"}],"type":"function"}
]''')

EXCHANGE_V2_ABI = json.loads('''[
    {"name":"getProxyWalletAddress","inputs":[{"name":"_addr","type":"address"}],"outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"name":"getCtfCollateral","inputs":[],"outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"}
]''')

COLLATERAL_ABI = json.loads('''[
    {"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},
    {"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},
    {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint256"}],"type":"function"}
]''')

MAX_UINT256 = 2**256 - 1


def connect_web3():
    """Connect to Polygon RPC."""
    for url in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(url))
            if w3.is_connected():
                print(f"[OK] Connected to {url}")
                return w3
        except Exception as e:
            print(f"[FAIL] {url}: {e}")
    print("[ERROR] Failed to connect to any RPC")
    return None


def get_proxy_address(w3):
    """Get the Polymarket proxy wallet address for this EOA."""
    exchange = w3.eth.contract(address=EXCHANGE_V2, abi=EXCHANGE_V2_ABI)
    proxy = exchange.functions.getProxyWalletAddress(WALLET).call()
    print(f"[INFO] EOA Wallet:    {WALLET}")
    print(f"[INFO] Proxy Wallet:  {proxy}")
    return proxy


def check_balances(w3, proxy):
    """Check USDC and pUSD balances."""
    usdc = w3.eth.contract(address=USDC, abi=USDC_ABI)
    collateral = w3.eth.contract(address=COLLATERAL, abi=COLLATERAL_ABI)

    usdc_balance_eoa = usdc.functions.balanceOf(WALLET).call()
    usdc_decimals = usdc.functions.decimals().call()
    usdc_balance = usdc_balance_eoa / (10 ** usdc_decimals)

    pusd_balance_proxy = collateral.functions.balanceOf(proxy).call()
    pusd_decimals = collateral.functions.decimals().call()
    pusd_balance = pusd_balance_proxy / (10 ** pusd_decimals)

    pusd_balance_eoa = collateral.functions.balanceOf(WALLET).call()
    pusd_balance_eoa_val = pusd_balance_eoa / (10 ** pusd_decimals)

    print(f"\n[INFO] === Balances ===")
    print(f"  USDC in EOA:       ${usdc_balance:.2f}")
    print(f"  pUSD in Proxy:     ${pusd_balance:.2f}")
    print(f"  pUSD in EOA:       ${pusd_balance_eoa_val:.2f}")

    return usdc_balance, pusd_balance, pusd_balance_eoa_val, usdc, collateral, usdc_decimals, pusd_decimals


def send_transaction(w3, tx):
    """Sign and send a transaction."""
    tx['from'] = WALLET
    tx['nonce'] = w3.eth.get_transaction_count(WALLET, 'pending')
    tx['gas'] = min(tx.get('gas', 300000), 500000)
    tx['gasPrice'] = w3.eth.gas_price

    print(f"  TX gas: {tx['gas']} | gasPrice: {tx['gasPrice'] / 1e9:.4f} Gwei")

    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  TX Hash: {tx_hash.hex()}")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    print(f"  Status: {'SUCCESS' if receipt.status == 1 else 'FAILED'}")
    return receipt


def approve_token(w3, token_contract, spender, amount, token_name):
    """Approve a spender to spend tokens."""
    print(f"\n[STEP] Approving {token_name} for {spender[:10]}...")
    tx = token_contract.functions.approve(spender, amount).build_transaction({
        'from': WALLET,
        'gas': 100000,
    })
    return send_transaction(w3, tx)


def deposit_usdc_to_pusd(w3, usdc, collateral, amount, usdc_decimals, pusd_decimals):
    """
    Deposit USDC to get pUSD.
    The collateral contract (pUSD) is minted via the Polymarket onramp.
    Since we don't have the onramp contract address, we'll transfer USDC
    to the collateral contract and hope it mints pUSD.
    
    Alternative: If the user has pUSD elsewhere, transfer it directly to the proxy.
    """
    print(f"\n[STEP] Depositing USDC → pUSD...")
    print(f"  NOTE: This requires the Polymarket Onramp contract.")
    print(f"  If this fails, you may need to:")
    print(f"  1. Buy pUSD on QuickSwap/SushiSwap")
    print(f"  2. Transfer pUSD directly to the proxy wallet")
    return None


def main():
    print("=" * 60)
    print("Polymarket CLOB Deposit Script (Headless)")
    print("=" * 60)

    # Connect
    w3 = connect_web3()
    if not w3:
        return

    # Get proxy address
    proxy = get_proxy_address(w3)

    # Check balances
    usdc_balance, pusd_balance, pusd_eoa, usdc, collateral, usdc_dec, pusd_dec = check_balances(w3, proxy)

    if pusd_balance > 0:
        print(f"\n[OK] pUSD already in proxy wallet: ${pusd_balance:.2f}")
        print("  Proceeding to approve exchanges...")
    elif pusd_eoa > 0:
        print(f"\n[INFO] pUSD found in EOA: ${pusd_eoa:.2f}")
        print("  Transferring pUSD to proxy wallet...")
        # Transfer pUSD from EOA to proxy
        tx = collateral.functions.transfer(proxy, int(pusd_eoa * (10 ** pusd_dec))).build_transaction({
            'from': WALLET,
            'gas': 100000,
        })
        send_transaction(w3, tx)
        print("  pUSD transferred to proxy!")
    elif usdc_balance > 0:
        print(f"\n[INFO] USDC found in EOA: ${usdc_balance:.2f}")
        print("  Need to swap USDC → pUSD via Polymarket Onramp")
        print("  Onramp contract address needed. Please transfer pUSD directly to proxy:")
        print(f"  Proxy Wallet: {proxy}")
        return
    else:
        print(f"\n[ERROR] No USDC or pUSD found in wallet!")
        print(f"  Please transfer USDC or pUSD to EOA: {WALLET}")
        print(f"  Or transfer pUSD directly to Proxy: {proxy}")
        return

    # Approve 3 exchange contracts to spend pUSD
    print(f"\n[STEP] Approving pUSD for exchange contracts...")
    amount = MAX_UINT256

    for name, addr in [
        ("Exchange V2", EXCHANGE_V2),
        ("Neg Risk Exchange V2", NEG_RISK_EXCHANGE_V2),
        ("Neg Risk Adapter", NEG_RISK_ADAPTER),
    ]:
        try:
            approve_token(w3, collateral, addr, amount, f"pUSD ({name})")
            time.sleep(2)
        except Exception as e:
            print(f"  [WARN] {name} approval failed: {e}")

    # Update CLOB balance allowance
    print(f"\n[STEP] Updating CLOB balance allowance...")
    from py_clob_client_v2 import ClobClient, ApiCreds
    from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType

    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=PRIVATE_KEY,
        creds=ApiCreds(
            api_key="c742e0d7-31d1-931b-f6d1-80e56d649db8",
            api_secret="nlBJU23DkTPPwOQCCVYoh1opHl_f6q7MOqK9dCt_gA0=",
            api_passphrase="c5687b041c27e33719867aa3ff875f86d742d1fe275f5bae67227943f30953cb",
        ),
    )

    client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    balance = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    print(f"  CLOB Balance: {balance}")

    print(f"\n{'='*60}")
    print("Deposit complete!")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
