import os, time, hmac, hashlib, base64, requests

# Load env
env = {}
with open('/opt/btc-polymarket-predictor/.env') as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'): continue
        if line.startswith('export '): line = line[7:]
        if '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")

ts = int(time.time())
secret = base64.b64decode(env['POLY_API_SECRET'])

# Cancel all orders
msg = f'{ts}POST/cancel-all'
sig = base64.b64encode(hmac.new(secret, msg.encode(), hashlib.sha256).digest()).decode()
r = requests.post('https://clob.polymarket.com/cancel-all', headers={
    'POLY_ADDRESS': env['POLY_DEPOSIT_WALLET'],
    'POLY_SIGNATURE': sig,
    'POLY_TIMESTAMP': str(ts),
    'POLY_API_KEY': env['POLY_API_KEY'],
    'POLY_PASSPHRASE': env['POLY_API_PASSPHRASE'],
}, timeout=10)
print(f'Cancel all: {r.status_code} {r.text[:200]}')

# Get balance
ts2 = int(time.time())
msg2 = f'{ts2}GET/balance-allowance?signature_type=3&asset_type=COLLATERAL'
sig2 = base64.b64encode(hmac.new(secret, msg2.encode(), hashlib.sha256).digest()).decode()
r2 = requests.get('https://clob.polymarket.com/balance-allowance?signature_type=3&asset_type=COLLATERAL', headers={
    'POLY_ADDRESS': env['POLY_DEPOSIT_WALLET'],
    'POLY_SIGNATURE': sig2,
    'POLY_TIMESTAMP': str(ts2),
    'POLY_API_KEY': env['POLY_API_KEY'],
    'POLY_PASSPHRASE': env['POLY_API_PASSPHRASE'],
}, timeout=10)
data = r2.json()
bal = float(data.get('balance', 0)) / 1e6
print(f'Balance: ${bal:.2f}')
