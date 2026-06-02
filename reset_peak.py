#!/usr/bin/env python3
"""Reset peak balance for new account"""
import os, json, sqlite3, time, hmac, hashlib, base64, requests

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

# Query real CLOB balance
ts = int(time.time())
secret = base64.b64decode(env['POLY_API_SECRET'])
msg = f"{ts}GET/balance-allowance?signature_type=3&asset_type=COLLATERAL"
sig = base64.b64encode(hmac.new(secret, msg.encode(), hashlib.sha256).digest()).decode()

headers = {
    'POLY_ADDRESS': env['POLY_DEPOSIT_WALLET'],
    'POLY_SIGNATURE': sig,
    'POLY_TIMESTAMP': str(ts),
    'POLY_API_KEY': env['POLY_API_KEY'],
    'POLY_PASSPHRASE': env['POLY_API_PASSPHRASE'],
}

r = requests.get(
    'https://clob.polymarket.com/balance-allowance?signature_type=3&asset_type=COLLATERAL',
    headers=headers, timeout=10
)
print(f"CLOB balance API: {r.status_code}")
if r.status_code == 200:
    data = r.json()
    balance = float(data.get('balance', 0)) / 1e6
    print(f"Balance: ${balance:.2f}")
else:
    print(f"Error: {r.text[:200]}")
    balance = 0

# Check current state
DB = '/opt/btc-polymarket-predictor/data/btc_predictor.db'
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

try:
    rows = conn.execute("SELECT * FROM state WHERE key IN ('peak_balance', 'start_balance')").fetchall()
    print("\nCurrent state:")
    for row in rows:
        print(f"  {row['key']}: {row['value']}")
except Exception as e:
    print(f"State query error: {e}")

# Reset peak to current balance
if balance > 0:
    try:
        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES ('peak_balance', ?)", (str(balance),))
        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES ('start_balance', ?)", (str(balance),))
        conn.commit()
        print(f"\nReset peak_balance and start_balance to ${balance:.2f}")
    except Exception as e:
        print(f"Reset error: {e}")

# Also check recent trades
try:
    trades = conn.execute("SELECT * FROM real_trades ORDER BY timestamp DESC LIMIT 5").fetchall()
    print(f"\nLast 5 real_trades ({len(trades)}):")
    for t in trades:
        print(f"  {dict(t)}")
except:
    print("\nNo real_trades table")

conn.close()
print("\nDone. Trader should pick up new balance next cycle.")
