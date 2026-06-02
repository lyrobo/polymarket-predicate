#!/usr/bin/env python3
"""Auto-redeem winning Polymarket positions. No MATIC gas needed (uses Relayer API)."""

import os, sys, json, time, logging
from web3 import Web3
from eth_account import Account
from eth_abi import encode as abi_encode

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

CTF = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
USDC = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
pUSD = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
NEG_RISK_ADAPTER = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")
RELAYER_URL = "https://relayer-v2.polymarket.com"
RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon-rpc.com",
]

def get_w3():
    for rpc in RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 5}))
            if w3.is_connected():
                return w3
        except:
            continue
    return Web3(Web3.HTTPProvider(RPCS[0], request_kwargs={"timeout": 10}))


def check_condition_resolved(w3, condition_id: str) -> bool:
    """Check if condition is resolved on-chain."""
    cid_bytes = bytes.fromhex(condition_id[2:] if condition_id.startswith("0x") else condition_id)
    selector = Web3.keccak(text="payoutDenominator(bytes32)")[:4]
    data = "0x" + (selector + abi_encode(["bytes32"], [cid_bytes])).hex()
    result = w3.eth.call({"to": CTF, "data": data})
    return int.from_bytes(result, "big") > 0


def build_redeem_calldata(condition_id: str) -> bytes:
    """Build redeemPositions calldata. Targets NegRiskAdapter for Polymarket 5-min markets."""
    selector = Web3.keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
    cid_hex = condition_id[2:] if condition_id.startswith("0x") else condition_id
    cid_bytes = bytes.fromhex(cid_hex)
    params = abi_encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [pUSD, b"\x00" * 32, cid_bytes, [1, 2]],
    )
    return selector + params


def report_payouts_onchain(w3, condition_id: str) -> bool:
    """Report resolved payout to CTF via NegRiskAdapter.

    Polymarket 5-min markets use negRisk. The UMA oracle resolves via the
    NegRiskAdapter, but the result must be explicitly reported to CTF before
    redeemPositions can transfer USDC.

    Returns True if report was sent (or already done).
    """
    private_key = os.getenv("POLY_PRIVATE_KEY")
    if not private_key:
        return False

    account = Account.from_key(private_key)
    cid_hex = condition_id[2:] if condition_id.startswith("0x") else condition_id
    cid_bytes = bytes.fromhex(cid_hex)

    # Check if already reported on CTF
    sel_den = Web3.keccak(text="payoutDenominator(bytes32)")[:4]
    data_den = "0x" + (sel_den + abi_encode(["bytes32"], [cid_bytes])).hex()
    try:
        den = int.from_bytes(w3.eth.call({"to": CTF, "data": data_den}), "big")
        if den > 0:
            return True  # Already reported
    except Exception:
        pass

    # Call NegRiskAdapter.reportPayouts(bytes32 conditionId)
    selector = Web3.keccak(text="reportPayouts(bytes32)")[:4]
    calldata = "0x" + (selector + abi_encode(["bytes32"], [cid_bytes])).hex()

    nonce = w3.eth.get_transaction_count(account.address, "pending")
    gas_price = w3.eth.gas_price
    tx = {
        "to": NEG_RISK_ADAPTER,
        "data": calldata,
        "from": account.address,
        "nonce": nonce,
        "gas": 200000,
        "gasPrice": gas_price,
        "chainId": 137,
        "value": 0,
    }

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    return receipt["status"] == 1


def redeem_via_relayer(w3, condition_id: str) -> dict:
    import requests
    
    private_key = os.getenv("POLY_PRIVATE_KEY")
    proxy_wallet = os.getenv("POLY_PROXY_WALLET", "")
    relayer_key = os.getenv("POLY_RELAYER_API_KEY", "")
    
    if not private_key:
        return {"success": False, "error": "POLY_PRIVATE_KEY not set"}
    
    account = Account.from_key(private_key)
    calldata = "0x" + build_redeem_calldata(condition_id).hex()
    
    # Build relay transaction payload
    payload = {
        "to": CTF,
        "value": "0",
        "data": calldata,
        "operation": "0",
        "safeTxGas": "0",
        "baseGas": "0",
        "gasPrice": "0",
        "gasToken": "0x0000000000000000000000000000000000000000",
        "refundReceiver": "0x0000000000000000000000000000000000000000",
    }
    
    # Sign as EOA owner of the proxy
    from eth_account.messages import encode_typed_data
    # Simple approach: use the shared relayer endpoint
    headers = {"Content-Type": "application/json"}
    if relayer_key:
        headers["RELAYER_API_KEY"] = relayer_key
        headers["RELAYER_API_KEY_ADDRESS"] = account.address
    
    # For proxy wallet (sig_type=3), we use the submit endpoint
    body = {
        "signer": account.address,
        "funder": proxy_wallet,
        "to": NEG_RISK_ADAPTER,
        "data": calldata,
        "value": "0",
        "signatureType": 3,
    }
    
    try:
        resp = requests.post(f"{RELAYER_URL}/submit", json=body, headers=headers, timeout=30)
        result = resp.json()
        tx_hash = result.get("transactionHash", "")
        if tx_hash:
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt["status"] == 1:
                return {"success": True, "tx_hash": tx_hash}
        return {"success": False, "error": str(result)}
    except Exception as e:
        return {"success": False, "error": str(e)}


def redeem_onchain(w3, condition_id: str) -> dict:
    """Fallback: on-chain redemption. Reports payouts via NegRiskAdapter first, then redeems CTF tokens for USDC."""
    private_key = os.getenv("POLY_PRIVATE_KEY")
    if not private_key:
        return {"success": False, "error": "No private key"}
    
    account = Account.from_key(private_key)
    matic_balance = w3.eth.get_balance(account.address)
    gas_price = w3.eth.gas_price
    estimated_cost = 500000 * gas_price  # 2 TXs: reportPayouts + redeemPositions
    
    if matic_balance < estimated_cost:
        return {
            "success": False,
            "error": f"Insufficient MATIC: {matic_balance / 1e18:.4f} < {estimated_cost / 1e18:.4f}"
        }
    
    # Step 1: Report payout to CTF via NegRiskAdapter
    logger.info(f"Reporting payout for {condition_id[:20]}...")
    reported = report_payouts_onchain(w3, condition_id)
    if reported:
        logger.info(f"Payout reported (or already done)")
    else:
        logger.warning(f"Payout report failed, trying redeem anyway")
    
    # Step 2: Redeem via NegRiskAdapter (handles USDC.e → pUSD conversion)
    calldata = build_redeem_calldata(condition_id)
    nonce = w3.eth.get_transaction_count(account.address, 'pending')
    tx = {
        "to": NEG_RISK_ADAPTER,
        "data": "0x" + calldata.hex(),
        "from": account.address,
        "nonce": nonce,
        "gas": 300000,
        "gasPrice": gas_price,
        "chainId": 137,
        "value": 0,
    }
    
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    
    if receipt["status"] == 1:
        return {"success": True, "tx_hash": tx_hash.hex()}
    return {"success": False, "error": "Transaction reverted"}


def get_redeemable_positions():
    """Query Polymarket data API for redeemable positions."""
    import requests
    private_key = os.getenv("POLY_PRIVATE_KEY")
    proxy = os.getenv("POLY_PROXY_WALLET", "")
    
    if not private_key:
        return []
    
    account = Account.from_key(private_key)
    wallets = [account.address.lower()]
    if proxy:
        wallets.append(proxy.lower())
    
    all_positions = []
    for addr in wallets:
        try:
            resp = requests.get(
                f"https://data-api.polymarket.com/positions?user={addr}",
                timeout=10
            )
            for p in resp.json():
                if p.get("redeemable"):
                    all_positions.append(p)
        except Exception as e:
            logger.warning(f"Query positions for {addr[:10]}... failed: {e}")
    
    return all_positions


def auto_redeem():
    """Main: find and redeem all winning positions."""
    positions = get_redeemable_positions()
    if not positions:
        logger.info("No redeemable positions found")
        return {"claimed": 0, "total": 0}
    
    # Deduplicate by condition_id
    seen = set()
    unique = []
    for p in positions:
        cid = p.get("conditionId", "")
        if cid and cid not in seen:
            seen.add(cid)
            unique.append(p)
    
    logger.info(f"Found {len(unique)} redeemable positions")
    
    w3 = get_w3()
    claimed = 0
    
    for i, p in enumerate(unique):
        condition_id = p["conditionId"]
        
        # Check if resolved on-chain
        if not check_condition_resolved(w3, condition_id):
            logger.debug(f"Condition {condition_id[:20]}... not resolved yet")
            continue
        
        # Try relayer first
        result = redeem_via_relayer(w3, condition_id)
        if not result["success"]:
            logger.info(f"Relayer failed for {condition_id[:20]}..., trying on-chain")
            result = redeem_onchain(w3, condition_id)
        
        if result["success"]:
            claimed += 1
            logger.info(f"✅ Redeemed {condition_id[:20]}... TX: {result.get('tx_hash', 'N/A')}")
        else:
            logger.warning(f"❌ Failed: {condition_id[:20]}... {result.get('error')}")
        
        if i < len(unique) - 1:
            time.sleep(3)  # Rate limit
    
    logger.info(f"Done: {claimed}/{len(unique)} positions redeemed")
    return {"claimed": claimed, "total": len(unique)}


if __name__ == "__main__":
    result = auto_redeem()
    print(json.dumps(result))
