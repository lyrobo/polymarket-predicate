#!/usr/bin/env python3
"""Check balances on 164"""
from web3 import Web3
import requests, json

# Use same RPC fallback as auto_redeem.py
RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon-rpc.com",
]
w3 = None
for rpc in RPCS:
    try:
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 5}))
        if w3.is_connected():
            print(f'Connected: {rpc}')
            break
    except:
        pass
if not w3:
    w3 = Web3(Web3.HTTPProvider(RPCS[0], request_kwargs={"timeout": 10}))

EOA = '0xEFfb586017871c64c7f42A3d088883666B2A38AB'
PROXY = '0x4B6b9BdBFe75F056E0b831b7EC6Ecf1926A8E6B1'

# MATIC balance
matic = w3.from_wei(w3.eth.get_balance(EOA), 'ether')
print(f'EOA MATIC: {matic}')

# pUSD balance on proxy
pusd_addr = '0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB'
pusd_abi = json.loads('[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]')
pusd = w3.eth.contract(address=pusd_addr, abi=pusd_abi)
try:
    bal = pusd.functions.balanceOf(PROXY).call()
    print(f'Proxy pUSD: {w3.from_wei(bal, "ether")}')
except Exception as e:
    print(f'pUSD error: {e}')

# Check positions
print('\nPositions:')
for addr in [PROXY, EOA]:
    resp = requests.get(f'https://gamma-api.polymarket.com/positions?user={addr}&limit=10', timeout=10)
    if resp.status_code == 200:
        positions = resp.json()
        print(f'  {addr[:10]}...: {len(positions)} positions')
        for p in positions[:5]:
            cid = p.get('conditionId', '')[:20]
            print(f'    {cid}... outcome={p.get("outcome","")} size={p.get("size","")}')
    else:
        print(f'  {addr[:10]}...: API {resp.status_code}')

# Check USDC on proxy
usdc_addr = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174'
usdc_abi = json.loads('[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]')
usdc = w3.eth.contract(address=usdc_addr, abi=usdc_abi)
try:
    bal = usdc.functions.balanceOf(PROXY).call()
    print(f'\nProxy USDC.e: {bal / 1e6}')
except Exception as e:
    print(f'USDC error: {e}')
