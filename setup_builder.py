#!/usr/bin/env python3
"""
Polymarket Builder + Relayer 初始化脚本 (v2 - 使用 SDK)
在 164 服务器上运行：python3 setup_builder.py
"""
import os, json, time, requests

# ==== 配置 ====
RELAYER_API_KEY = "019e5a36-1f98-7dcc-afaf-eced5704a1be"
RELAYER_URL = "https://relayer-v2.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
CHAIN_ID = 137

# 从 .env 加载
env = {}
with open(".env") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:]
        if '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")

PRIVATE_KEY = env.get('POLY_PRIVATE_KEY', '')
PROXY_WALLET = env.get('POLY_PROXY_WALLET', '')

print("=" * 60)
print("Polymarket Builder + Relayer Setup v2")
print("=" * 60)
print(f"Private Key: {'✓' if PRIVATE_KEY else '✗ MISSING'}")
print(f"Proxy Wallet: {PROXY_WALLET}")
print(f"Relayer Key: {RELAYER_API_KEY[:20]}...")

if not PRIVATE_KEY:
    print("\n❌ POLY_PRIVATE_KEY not set in .env!")
    exit(1)

# ==== Step 1: Derive CLOB API credentials using SDK ====
print("\n📋 Step 1: Deriving CLOB API credentials (using SDK)...")

from py_clob_client.client import ClobClient
from py_clob_client.signer import Signer

# Initialize Signer with private key
signer = Signer._create(PRIVATE_KEY, CHAIN_ID)

# Create ClobClient for L1 auth only (no creds yet)
clob = ClobClient(
    host=CLOB_URL,
    chain_id=CHAIN_ID,
    signer=signer,
)

try:
    creds = clob.create_or_derive_api_creds()
    if creds:
        api_key = creds.api_key
        secret = creds.api_secret
        passphrase = creds.api_passphrase
        print(f"  ✅ API Key: {api_key}")
        print(f"  ✅ Secret: {secret[:20]}...")
        print(f"  ✅ Passphrase: {passphrase[:20]}...")
    else:
        print("  ❌ create_or_derive_api_creds returned None")
        exit(1)
except Exception as e:
    print(f"  ❌ Failed: {e}")
    exit(1)

# ==== Step 2: Update .env ====
print("\n📋 Step 2: Updating .env...")

new_lines = []
for line in open(".env").readlines():
    stripped = line.strip()
    if stripped.startswith('export POLY_API_KEY='):
        new_lines.append(f'export POLY_API_KEY={api_key}\n')
    elif stripped.startswith('export POLY_API_SECRET='):
        new_lines.append(f'export POLY_API_SECRET={secret}\n')
    elif stripped.startswith('export POLY_API_PASSPHRASE='):
        new_lines.append(f'export POLY_API_PASSPHRASE={passphrase}\n')
    elif stripped.startswith('export POLY_RELAYER_API_KEY='):
        new_lines.append(f'export POLY_RELAYER_API_KEY={RELAYER_API_KEY}\n')
    else:
        new_lines.append(line)

with open(".env", "w") as f:
    f.writelines(new_lines)
print("  ✅ .env updated")

# ==== Step 3: Test Relayer Auth ====
print("\n📋 Step 3: Testing Relayer auth...")

from eth_account import Account
account = Account.from_key(PRIVATE_KEY)

rh = {
    "Content-Type": "application/json",
    "RELAYER_API_KEY": RELAYER_API_KEY,
    "RELAYER_API_KEY_ADDRESS": account.address,
}

# Deploy deposit wallet
body = {
    "type": "WALLET-CREATE",
    "from": account.address,
    "to": "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07",
}
resp = requests.post(f"{RELAYER_URL}/submit", json=body, headers=rh, timeout=10)
if resp.status_code == 200:
    result = resp.json()
    tx_id = result.get('transactionID', '')
    state = result.get('state', '')
    print(f"  WALLET-CREATE submitted: {state} (tx: {tx_id})")
    
    # Poll for confirmation
    for i in range(10):
        time.sleep(5)
        r2 = requests.get(f"{RELAYER_URL}/transaction/{tx_id}", headers=rh, timeout=10)
        if r2.status_code == 200:
            tx = r2.json()
            s = tx.get('state', '')
            print(f"    [{i+1}] State: {s}")
            if s in ('STATE_CONFIRMED', 'STATE_MINED'):
                addr = tx.get('depositWalletAddress', tx.get('depositWallet', ''))
                print(f"  ✅ Deposit wallet deployed: {addr}")
                break
            elif s == 'STATE_FAILED':
                print(f"  ⚠️  Deployment failed: {json.dumps(tx, indent=2)[:500]}")
                break
    else:
        print(f"  ⏳ Timed out waiting for confirmation")
else:
    print(f"  ❌ Relayer auth: {resp.status_code} {resp.text}")

# ==== Step 4: Test CLOB Balance ====
print("\n📋 Step 4: Testing CLOB balance...")

from py_clob_client.clob_types import ApiCreds

try:
    clob2 = ClobClient(
        host=CLOB_URL,
        chain_id=CHAIN_ID,
        signer=signer,
        creds=ApiCreds(api_key=api_key, api_secret=secret, api_passphrase=passphrase),
        signature_type=3,
        funder_address=PROXY_WALLET,
    )
    from py_clob_client.clob_types import AssetType
    balance = clob2.get_balance_allowance(asset_type=AssetType.COLLATERAL)
    print(f"  ✅ Balance query successful")
    print(f"  {json.dumps(balance, indent=2)}")
except Exception as e:
    print(f"  ⚠️  Balance error: {e}")

print("\n" + "=" * 60)
print("Setup complete!")
print("=" * 60)
