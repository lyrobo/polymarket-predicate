import json, sqlite3

# Check state
with open('/opt/btc-polymarket-predictor/data/risk_state.json') as f:
    state = json.load(f)
print('risk_state.json:', state)

# Reset it
with open('/opt/btc-polymarket-predictor/data/risk_state.json', 'w') as f:
    json.dump({}, f)
print('Reset to {}')

# Check DB
conn = sqlite3.connect('/opt/btc-polymarket-predictor/data/btc_predictor.db')
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print('Tables:', [t[0] for t in tables])

for table in ['real_trades', 'portfolio_snapshots']:
    try:
        r = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()
        print(f'{table}: {r[0]} rows')
    except:
        pass

# Check latest trades for PnL
try:
    rows = conn.execute('SELECT id, pnl, status, timestamp FROM real_trades ORDER BY id DESC LIMIT 10').fetchall()
    print('\nLast 10 trades:')
    for r in rows:
        print(f'  #{r[0]} PnL={r[1]:.4f} status={r[2]} time={r[3]}')
except Exception as e:
    print(f'Trade query error: {e}')

conn.close()
