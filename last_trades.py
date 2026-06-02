import sqlite3
conn = sqlite3.connect('/opt/btc-polymarket-predictor/data/btc_predictor.db')

rows = conn.execute('SELECT id, direction, size, pnl, status, timestamp FROM real_trades ORDER BY id DESC LIMIT 10').fetchall()
for r in rows:
    pnl = f'{r[3]:.2f}' if r[3] is not None else 'N/A'
    print(f'#{r[0]:<5} {r[1]:4s} size={r[2]:7.1f} PnL={pnl:>8s} {r[4]:10s} {r[5]}')

# Summary
total = conn.execute('SELECT COUNT(*) FROM real_trades').fetchone()[0]
settled = conn.execute("SELECT COUNT(*) FROM real_trades WHERE status='settled'").fetchone()[0]
print(f'\nTotal: {total}, Settled: {settled}')
conn.close()
