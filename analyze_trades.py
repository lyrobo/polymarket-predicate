import sqlite3

conn = sqlite3.connect('/opt/btc-polymarket-predictor/data/btc_predictor.db')

# Overall stats
total = conn.execute('SELECT COUNT(*) FROM real_trades').fetchone()[0]
wins = conn.execute("SELECT COUNT(*) FROM real_trades WHERE pnl > 0").fetchone()[0]
losses = conn.execute("SELECT COUNT(*) FROM real_trades WHERE pnl < 0").fetchone()[0]
settled = conn.execute("SELECT COUNT(*) FROM real_trades WHERE status='settled'").fetchone()[0]
print(f'Total: {total} | Settled: {settled} | Wins: {wins} | Losses: {losses}')
if wins + losses > 0:
    print(f'Win rate: {wins/(wins+losses)*100:.1f}%')

# Last 30 trades
rows = conn.execute('SELECT id, pnl, status, timestamp FROM real_trades ORDER BY id DESC LIMIT 30').fetchall()
print('\nLast 30:')
for r in rows:
    pnl_str = f'{r[1]:.4f}' if r[1] is not None else 'N/A'
    print(f'  #{r[0]} PnL={pnl_str} status={r[2]} time={r[3]}')

# Total PnL
total_pnl = conn.execute('SELECT SUM(pnl) FROM real_trades WHERE pnl IS NOT NULL').fetchone()[0] or 0
print(f'\nTotal PnL: ${total_pnl:.2f}')

# Recent 100
last100 = conn.execute('SELECT pnl FROM real_trades WHERE pnl IS NOT NULL ORDER BY id DESC LIMIT 100').fetchall()
w100 = sum(1 for r in last100 if r[0] > 0)
l100 = sum(1 for r in last100 if r[0] < 0)
if w100 + l100 > 0:
    print(f'Last 100: W={w100} L={l100} rate={w100/(w100+l100)*100:.1f}%')

# By direction
for direction in ['UP', 'DN']:
    d_wins = conn.execute(f"SELECT COUNT(*) FROM real_trades WHERE pnl > 0 AND direction='{direction}'").fetchone()[0]
    d_losses = conn.execute(f"SELECT COUNT(*) FROM real_trades WHERE pnl < 0 AND direction='{direction}'").fetchone()[0]
    d_total = d_wins + d_losses
    if d_total > 0:
        print(f'{direction}: W={d_wins} L={d_losses} rate={d_wins/d_total*100:.1f}%')

conn.close()
