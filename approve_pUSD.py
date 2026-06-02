"""
Approve pUSD for Polymarket Exchange Contracts (Gnosis Safe Proxy)
===================================================================
Execute approval transactions through the Polymarket proxy wallet.
The proxy wallet is a Gnosis Safe owned by the EOA.
"""
import json
import time
from web3 import Web3

PRIVATE_KEY = "0x75d434252c68a3a4272beda3d8bd8e279d4c4b9c51fb6c7066ee083374fbca0a"
EOA_ADDRESS = "0x27c66a42DDb2EC9f1db2361447d551371cC06bac"
PROXY_SAFE = "0xaC447078c16184016C6eF8BE97a1FA963b26Ff46"
PUSD_TOKEN = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CHAIN_ID = 137

# 3 core exchange contracts to approve
EXCHANGES = [
    ("Exchange V2", "0xE111180000d2663C0091e4f400237545B87B996B"),
    ("Neg Risk Exchange V2", "0xe2222d279d744050d28e00520010520000310F59"),
    ("Neg Risk Adapter", "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
]

# Gnosis Safe ABI (minimal for execTransaction)
SAFE_ABI = [
    {
        "name": "execTransaction",
        "type": "function",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "signatures", "type": "bytes"}
        ],
        "outputs": [{"name": "success", "type": "bool"}],
        "stateMutability": "payable"
    },
    {
        "name": "nonce",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view"
    },
    {
        "name": "getTransactionHash",
        "type": "function",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "_nonce", "type": "uint256"}
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "view"
    }
]

MAX_UINT256 = 2**256 - 1


def connect_web3():
    rpc_urls = ["https://1rpc.io/matic", "https://polygon-rpc.com"]
    for url in rpc_urls:
        try:
            w3 = Web3(Web3.HTTPProvider(url))
            if w3.is_connected():
                print(f"[OK] Connected to {url}")
                return w3
        except:
            continue
    print("[ERROR] Failed to connect")
    return None


def build_approve_data(spender, amount):
    """Build ERC20 approve function call data."""
    from eth_abi import encode
    selector = Web3.keccak(text="approve(address,uint256)")[:4]
    encoded = encode(['address', 'uint256'], [Web3.to_checksum_address(spender), amount])
    return selector + encoded


def sign_and_execute(w3, safe, to, value, data, operation, nonce):
    """Sign and execute a transaction on the Safe."""
    # Get transaction hash
    tx_hash = safe.functions.getTransactionHash(
        Web3.to_checksum_address(to),
        value,
        data,
        operation,
        0,  # safeTxGas
        0,  # baseGas
        0,  # gasPrice
        "0x0000000000000000000000000000000000000000",  # gasToken
        "0x0000000000000000000000000000000000000000",  # refundReceiver
        nonce
    ).call()
    
    print(f"  TX Hash to sign: {tx_hash.hex()}")
    
    # Sign with EOA
    from eth_account.messages import encode_defunct
    signature = w3.eth.account.sign_message(encode_defunct(hexstr=tx_hash.hex()), private_key=PRIVATE_KEY)
    
    # Format signature for Gnosis Safe: r + s + v
    # For EOA signatures, v is adjusted to 1 (or 27+4=31 for eth_sign)
    # Here we use standard signature with v=1
    sig = signature.signature
    sig_bytes = bytearray(sig)
    sig_bytes[-1] = 1  # Set v to 1 for EOA signature in Safe
    
    signatures = bytes(sig_bytes)
    
    # Execute transaction
    tx = safe.functions.execTransaction(
        Web3.to_checksum_address(to),
        value,
        data,
        operation,
        0,  # safeTxGas
        0,  # baseGas
        0,  # gasPrice
        "0x0000000000000000000000000000000000000000",
        "0x0000000000000000000000000000000000000000",
        signatures
    ).build_transaction({
        'from': EOA_ADDRESS,
        'nonce': w3.eth.get_transaction_count(EOA_ADDRESS, 'pending'),
        'gas': 500000,
        'gasPrice': w3.eth.gas_price,
        'chainId': CHAIN_ID,
    })
    
    print(f"  Gas: {tx['gas']} | Gas Price: {tx['gasPrice'] / 1e9:.4f} Gwei")
    
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  TX Hash: {tx_hash.hex()}")
    
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    print(f"  Status: {'SUCCESS' if receipt.status == 1 else 'FAILED'}")
    return receipt


def main():
    print("=" * 60)
    print("Approve pUSD for Polymarket Exchange Contracts")
    print("=" * 60)
    
    w3 = connect_web3()
    if not w3:
        return
    
    safe = w3.eth.contract(address=Web3.to_checksum_address(PROXY_SAFE), abi=SAFE_ABI)
    
    # Get current nonce
    try:
        nonce = safe.functions.nonce().call()
        print(f"[INFO] Safe nonce: {nonce}")
    except Exception as e:
        print(f"[ERROR] Failed to get nonce: {e}")
        return
    
    # Approve each exchange
    for name, addr in EXCHANGES:
        print(f"\n[STEP] Approving {name} ({addr[:10]}...)")
        
        # Build approve data
        approve_data = build_approve_data(addr, MAX_UINT256)
        
        try:
            sign_and_execute(w3, safe, PUSD_TOKEN, 0, approve_data, 0, nonce)
            nonce += 1
            time.sleep(3)
        except Exception as e:
            print(f"  [ERROR] Approval failed: {e}")
            import traceback
            traceback.print_exc()
            return
    
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
    print("Approval complete!")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
