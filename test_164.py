#!/usr/bin/env python3
"""Test Relayer + CLOB on 164"""
import os, json, time, requests
from eth_account import Account
from eth_account.messages import encode_typed_data

# Load .env
env = {}
env_path = '/opt/btc-polymarket-predictor/.env'
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'): continue
        if line.startswith('export '): line = line[7:]
        if '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")

PRIVATE_KEY = env.get('POLY_PRIVATE_KEY')
RELAYER_KEY = env.get('POLY_RELAYER_API_KEY')
PROXY = env.get('POLY_PROXY_WALLET')
API_KEY = env.get('POLY_API_KEY')
API_SECRET = env.get('POLY_API_SECRET')
API_PASSPHRASE = env.get('POLY_API_PASSPHRASE')

account = Account.from_key(PRIVATE_KEY)
print(f'EOA: {account.address}')
print(f'Proxy: {PROXY}')
print(f'API Key: {API_KEY}')

# === 1. Test Relayer: WALLET-CREATE ===
print('\n=== Relayer Test ===')
rh = {
    'Content-Type': 'application/json',
    'RELAYER_API_KEY': RELAYER_KEY,
    'RELAYER_API_KEY_ADDRESS': account.address,
}
body = {
    'type': 'WALLET-CREATE',
    'from': account.address,
    'to': '0x00000000000Fb5C9ADea0298D729A0CB3823Cc07',
}
resp = requests.post('https://relayer-v2.polymarket.com/submit', json=body, headers=rh, timeout=10)
print(f'WALLET-CREATE: {resp.status_code} {resp.text[:300]}')

# === 2. Test CLOB: derive-api-key ===
print('\n=== CLOB Auth Test ===')
timestamp = int(time.time())
domain = {'name': 'ClobAuthDomain', 'version': '1', 'chainId': 137}
types = {'ClobAuth': [
    {'name': 'address', 'type': 'address'},
    {'name': 'timestamp', 'type': 'string'},
    {'name': 'nonce', 'type': 'uint256'},
    {'name': 'message', 'type': 'string'},
]}
msg = {
    'address': account.address,
    'timestamp': str(timestamp),
    'nonce': 0,
    'message': 'This message attests that I control the given wallet',
}
encoded = encode_typed_data(full_message={
    'types': {
        'EIP712Domain': [
            {'name': 'name', 'type': 'string'},
            {'name': 'version', 'type': 'string'},
            {'name': 'chainId', 'type': 'uint256'},
        ],
        **types,
    },
    'primaryType': 'ClobAuth',
    'domain': domain,
    'message': msg,
})
sig = account.sign_message(encoded).signature.hex()
headers = {
    'POLY_ADDRESS': account.address,
    'POLY_SIGNATURE': '0x' + sig,
    'POLY_TIMESTAMP': str(timestamp),
    'POLY_NONCE': '0',
}
resp2 = requests.get('https://clob.polymarket.com/auth/derive-api-key', headers=headers, timeout=10)
print(f'Derive API Key: {resp2.status_code}')
if resp2.status_code == 200:
    creds = resp2.json()
    print(f'  apiKey: {creds.get("apiKey")}')
    print(f' Match env? {"YES" if creds.get("apiKey") == API_KEY else "NO (different from .env)"}')
else:
    print(f'  {resp2.text[:200]}')

print('\nDone.')
