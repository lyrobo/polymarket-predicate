import sqlite3
conn = sqlite3.connect('/opt/btc-polymarket-predictor/data/btc_predictor.db')

# Status breakdown
for s in ['settled', 'cancelled', 'pending', 'void', 'open', 'filled', 'expired']:
    cnt = conn.execute("SELECT COUNT(*) FROM real_trades WHERE status=?", (s,)).fetchone()[0]
    if cnt > 0:
        print(f'{s}: {cnt}')

# Settled trades
rows = conn.execute("SELECT id, direction, size, pnl, timestamp FROM real_trades WHERE status='settled' ORDER BY id").fetchall()
print(f'\nSettled trades ({len(rows)}):')
for r in rows:
    print(f'  #{r[0]} {r[1]} size={r[2]} PnL={r[3]:.2f} time={r[4]}')

# Total
total_pnl = conn.execute("SELECT SUM(pnl) FROM real_trades WHERE pnl IS NOT NULL").fetchone()[0] or 0
print(f'\nTotal PnL: ${total_pnl:.2f}')

# Check cancelled reasons - look at last 100 cancelled
cancelled = conn.execute("SELECT id, direction, size, timestamp FROM real_trades WHERE status='cancelled' ORDER BY id DESC LIMIT 100").fetchall()
print(f'\nLast 10 cancelled ({len(cancelled)} total recent):')
for r in cancelled[:10]:
    print(f'  #{r[0]} {r[1]} size={r[2]} time={r[3]}')

conn.close()
