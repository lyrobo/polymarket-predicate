import os, time, hmac, hashlib, base64, requests

env = {}
with open('/opt/btc-polymarket-predictor/.env') as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'): continue
        if line.startswith('export '): line = line[7:]
        if '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")

# Fix base64 padding
raw = env['POLY_API_SECRET']
raw = raw + '=' * (4 - len(raw) % 4) if len(raw) % 4 else raw
secret = base64.b64decode(raw)

def sign(method, path):
    ts = int(time.time())
    msg = f'{ts}{method}{path}'
    sig = base64.b64encode(hmac.new(secret, msg.encode(), hashlib.sha256).digest()).decode()
    return ts, sig

def headers(ts, sig):
    return {
        'POLY_ADDRESS': env['POLY_DEPOSIT_WALLET'],
        'POLY_SIGNATURE': sig,
        'POLY_TIMESTAMP': str(ts),
        'POLY_API_KEY': env['POLY_API_KEY'],
        'POLY_PASSPHRASE': env['POLY_API_PASSPHRASE'],
    }

# Cancel all
ts, sig = sign('POST', '/cancel-all')
r = requests.post('https://clob.polymarket.com/cancel-all', headers=headers(ts, sig), timeout=10)
print(f'Cancel all: {r.status_code} {r.text[:200]}')

# Balance
ts2, sig2 = sign('GET', '/balance-allowance?signature_type=3&asset_type=COLLATERAL')
r2 = requests.get('https://clob.polymarket.com/balance-allowance?signature_type=3&asset_type=COLLATERAL', headers=headers(ts2, sig2), timeout=10)
d = r2.json()
print(f'Balance: ${float(d.get("balance", 0)) / 1e6:.2f}')
