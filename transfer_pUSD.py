"""
Transfer pUSD from Polymarket Proxy Wallet to EOA
==================================================
The proxy wallet (0xaC447078...) is a Gnosis Safe owned by the EOA.
The EOA can execute transactions on the Safe.

Usage:
    python3 transfer_pUSD.py
"""
import json
import time
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# Polygon RPC
POLYGON_RPC = "https://1rpc.io/matic"

# Addresses
EOA_ADDRESS = "0x27c66a42DDb2EC9f1db2361447d551371cC06bac"
PROXY_SAFE_ADDRESS = "0xaC447078c16184016C6eF8BE97a1FA963b26Ff46"
PUSD_TOKEN_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

# Gnosis Safe ABI (minimal)
SAFE_ABI = [
    {"name":"execTransaction","type":"function","inputs":[
        {"name":"to","type":"address"},
        {"name":"value","type":"uint256"},
        {"name":"data","type":"bytes"},
        {"name":"operation","type":"uint8"},
        {"name":"safeTxGas","type":"uint256"},
        {"name":"baseGas","type":"uint256"},
        {"name":"gasPrice","type":"uint256"},
        {"name":"gasToken","type":"address"},
        {"name":"refundReceiver","type":"address"},
        {"name":"signatures","type":"bytes"}
    ],"outputs":[{"name":"success","type":"bool"}],"stateMutability":"payable"},
    {"name":"nonce","type":"function","inputs":[],"outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"},
    {"name":"getTransactionHash","type":"function","inputs":[
        {"name":"to","type":"address"},
        {"name":"value","type":"uint256"},
        {"name":"data","type":"bytes"},
        {"name":"operation","type":"uint8"},
        {"name":"safeTxGas","type":"uint256"},
        {"name":"baseGas","type":"uint256"},
        {"name":"gasPrice","type":"uint256"},
        {"name":"gasToken","type":"address"},
        {"name":"refundReceiver","type":"address"},
        {"name":"_nonce","type":"uint256"}
    ],"outputs":[{"name":"","type":"bytes32"}],"stateMutability":"view"},
    {"name":"domainSeparator","type":"function","inputs":[],"outputs":[{"name":"","type":"bytes32"}],"stateMutability":"view"},
    {"name":"approvedSigners","type":"mapping","inputs":[{"name":"","type":"address"}],"outputs":[{"name":"","type":"bool"}],"stateMutability":"view"},
    {"name":"isOwner","type":"function","inputs":[{"name":"_owner","type":"address"}],"outputs":[{"name":"","type":"bool"}],"stateMutability":"view"},
    {"name":"getOwners","type":"function","inputs":[],"outputs":[{"name":"","type":"address[]"}],"stateMutability":"view"},
    {"name":"threshold","type":"function","inputs":[],"outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"},
]

# ERC20 transfer ABI
ERC20_TRANSFER_ABI = [
    {"name":"transfer","type":"function","inputs":[
        {"name":"_to","type":"address"},
        {"name":"_value","type":"uint256"}
    ],"outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable"},
]

# Polymarket Proxy Factory address (for deriving proxy)
POLY_PROXY_FACTORY = "0x31f7882AE2Aa9b0057D163Dc9D2C0f01F3cE1e77"  # Common on Polygon

import urllib.request
import json
from web3 import Web3
from eth_account.messages import encode_defunct

PRIVATE_KEY = "0x75d434252c68a3a4272beda3d8bd8e279d4c4b9c51fb6c7066ee083374fbca0a"
EOA_ADDRESS = "0x27c66a42DDb2EC9f1db2361447d551371cC06bac"
CHAIN_ID = 137


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


def get_pusd_balance(w3, address):
    pusd = w3.eth.contract(address=w3.to_checksum_address(PUSD_TOKEN_ADDRESS), abi=[
        {"name":"balanceOf","inputs":[{"name":"account","type":"address"}],"outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
        {"name":"decimals","inputs":[],"outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    ])
    balance = pusd.functions.balanceOf(w3.to_checksum_address(address)).call()
    decimals = pusd.functions.decimals().call()
    return balance / (10 ** decimals), balance, decimals


def build_transfer_data(to_address, amount_wei):
    """Build ERC20 transfer function call data."""
    from eth_abi import encode
    from web3 import Web3
    # transfer(address, uint256)
    selector = Web3.keccak(text="transfer(address,uint256)")[:4]
    encoded = encode(['address', 'uint256'], [Web3.to_checksum_address(to_address), amount_wei])
    return selector + encoded


def sign_safe_transaction(w3, safe_address, to_address, value, data, operation=0):
    """Sign a Gnosis Safe transaction with the EOA owner."""
    safe = w3.eth.contract(address=w3.to_checksum_address(safe_address), abi=SAFE_ABI)
    
    # Get nonce
    nonce = safe.functions.nonce().call()
    
    # Get transaction hash
    tx_hash = safe.functions.getTransactionHash(
        w3.to_checksum_address(to_address),
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
    
    print(f"  TX Hash: {tx_hash.hex()}")
    print(f"  Nonce: {nonce}")
    
    # Sign with EOA
    from eth_account.messages import encode_defunct
    signature = w3.eth.account.sign_message(encode_defunct(hexstr=tx_hash.hex()), private_key=PRIVATE_KEY)
    
    # Format signature: r (32 bytes) + s (32 bytes) + v (1 byte)
    # For Gnosis Safe, we need to append the signature in the format:
    # owner_address (20 bytes) + signature_length (32 bytes) + r (32 bytes) + s (32 bytes) + v (1 byte)
    # But for single owner, just use the signature directly with v adjusted
    
    # Gnosis Safe expects: r + s + v where v = 1 (for EOA signature)
    sig = signature.signature
    # Adjust v for Gnosis Safe (v = 1 means EOA signature)
    sig_bytes = bytearray(sig)
    sig_bytes[-1] = 1  # Set v to 1 for EOA
    
    return bytes(sig_bytes), nonce


def execute_safe_transaction(w3, safe_address, to_address, value, data, signatures, nonce):
    """Execute the signed transaction on the Safe."""
    safe = w3.eth.contract(address=w3.to_checksum_address(safe_address), abi=SAFE_ABI)
    
    # Build the execTransaction call
    tx = safe.functions.execTransaction(
        w3.to_checksum_address(to_address),
        value,
        data,
        0,  # operation (0 = call)
        0,  # safeTxGas
        0,  # baseGas
        0,  # gasPrice
        "0x0000000000000000000000000000000000000000",  # gasToken
        "0x0000000000000000000000000000000000000000",  # refundReceiver
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
    print("Transfer pUSD from Polymarket Proxy Wallet")
    print("=" * 60)
    
    w3 = connect_web3()
    if not w3:
        return
    
    proxy_address = "0xaC447078c16184016C6eF8BE97a1FA963b26Ff46"
    target_address = EOA_ADDRESS  # Transfer back to EOA
    
    # Check current balance
    print(f"\n[INFO] Proxy Wallet: {proxy_address}")
    print(f"[INFO] Target Address: {target_address}")
    
    balance, balance_wei, decimals = get_pusd_balance(w3, proxy_address)
    print(f"[INFO] pUSD Balance: ${balance:.2f}")
    
    if balance <= 0:
        print("[ERROR] No pUSD to transfer!")
        return
    
    # Build transfer data
    transfer_data = build_transfer_data(target_address, balance_wei)
    print(f"\n[STEP] Building transfer transaction...")
    print(f"  Transfer {balance:.2f} pUSD to {target_address}")
    
    # Sign the transaction
    print(f"\n[STEP] Signing transaction...")
    signatures, nonce = sign_safe_transaction(
        w3, proxy_address, PUSD_TOKEN_ADDRESS, 0, transfer_data
    )
    
    # Execute
    print(f"\n[STEP] Executing transaction...")
    try:
        receipt = execute_safe_transaction(
            w3, proxy_address, PUSD_TOKEN_ADDRESS, 0, transfer_data, signatures, nonce
        )
        
        # Check final balance
        time.sleep(2)
        final_balance, _, _ = get_pusd_balance(w3, proxy_address)
        eoa_balance, _, _ = get_pusd_balance(w3, target_address)
        
        print(f"\n{'='*60}")
        print("Transfer complete!")
        print(f"  pUSD in Proxy: ${final_balance:.2f}")
        print(f"  pUSD in EOA:   ${eoa_balance:.2f}")
        print(f"{'='*60}")
        
    except Exception as e:
        print(f"\n[ERROR] Transaction failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
