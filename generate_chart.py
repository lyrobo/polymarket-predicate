#!/usr/bin/env python3
"""Generate balance chart from sim_portfolio data."""

import sqlite3
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime
import numpy as np

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
DB_PATH = os.path.join(DATA_DIR, 'btc_predictor.db')
OUTPUT = os.path.join(DATA_DIR, 'balance_chart.png')

conn = sqlite3.connect(DB_PATH)

rows = conn.execute('''
    SELECT timestamp, balance, total_value, total_pnl, total_return_pct,
           max_drawdown_pct, win_rate, wins, losses, total_trades, open_positions
    FROM sim_portfolio ORDER BY id ASC
''').fetchall()

trades = conn.execute('''
    SELECT timestamp, pnl, win, direction, amount
    FROM sim_trades WHERE status="resolved" ORDER BY id ASC
''').fetchall()

conn.close()

if not rows:
    print("No data")
    sys.exit(1)

timestamps = []
balances = []
values = []
pnls = []
returns = []
drawdowns = []
win_rates = []
total_trades_list = []

for r in rows:
    ts = datetime.fromisoformat(r[0])
    timestamps.append(ts)
    balances.append(r[1])
    values.append(r[2])
    pnls.append(r[3])
    returns.append(r[4])
    drawdowns.append(r[5])
    win_rates.append(r[6])
    total_trades_list.append(r[9])

fig, axes = plt.subplots(3, 2, figsize=(18, 16))
fig.suptitle('BTC 5-Min Simulated Trading - Balance Dashboard ($100 USDT, Max $1000/bet)', 
             fontsize=18, fontweight='bold', y=0.98, color='#e1e8f0')

bg = '#0a0e17'
grid = '#1e2a3a'
txt = '#e1e8f0'
up = '#00e676'
dn = '#ff5252'
acc = '#00d4ff'
pur = '#e040fb'
yel = '#ffd740'

for ax in axes.flat:
    ax.set_facecolor(bg)
    for spine in ax.spines.values():
        spine.set_color(grid)
    ax.tick_params(colors=txt)
    for lbl in ax.get_xticklabels() + ax.get_yticklabels():
        lbl.set_color(txt)

# 1. Balance
ax1 = axes[0, 0]
ax1.plot(timestamps, balances, color=acc, linewidth=2, label='Balance')
ax1.plot(timestamps, values, color=pur, linewidth=2, label='Total Value')
ax1.fill_between(timestamps, balances, values, alpha=0.1, color=pur)
ax1.axhline(y=100, color=yel, linestyle='--', linewidth=1, alpha=0.5, label='Initial $100')
ax1.set_title('Balance & Total Value', fontsize=14, fontweight='bold', color=txt)
ax1.set_ylabel('USDT', color=txt)
ax1.legend(loc='upper left', facecolor=bg, edgecolor=grid, labelcolor=txt)
ax1.grid(True, alpha=0.2, color=grid)
ax1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))

# 2. PnL
ax2 = axes[0, 1]
ax2.plot(timestamps, pnls, color=acc, linewidth=2)
ax2.fill_between(timestamps, pnls, 0, alpha=0.3, where=[p>=0 for p in pnls], color=up, interpolate=True)
ax2.fill_between(timestamps, pnls, 0, alpha=0.3, where=[p<0 for p in pnls], color=dn, interpolate=True)
ax2.set_title('Cumulative P&L', fontsize=14, fontweight='bold', color=txt)
ax2.set_ylabel('USDT', color=txt)
ax2.axhline(y=0, color=txt, linestyle='-', linewidth=0.5, alpha=0.3)
ax2.grid(True, alpha=0.2, color=grid)
ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))

# 3. Return %
ax3 = axes[1, 0]
ax3.plot(timestamps, returns, color=up, linewidth=2)
ax3.fill_between(timestamps, returns, 0, alpha=0.2, color=up)
ax3.set_title('Return Percentage', fontsize=14, fontweight='bold', color=txt)
ax3.set_ylabel('%', color=txt)
ax3.axhline(y=0, color=txt, linestyle='-', linewidth=0.5, alpha=0.3)
ax3.grid(True, alpha=0.2, color=grid)
ax3.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))

# 4. Win Rate
ax4 = axes[1, 1]
ax4.plot(timestamps, win_rates, color=yel, linewidth=2, label='Win Rate')
ax4.axhline(y=50, color=dn, linestyle='--', linewidth=1, alpha=0.5, label='Random (50%)')
ax4.set_title('Win Rate', fontsize=14, fontweight='bold', color=txt)
ax4.set_ylabel('%', color=txt)
ax4.set_ylim(0, 105)
ax4.legend(loc='upper left', facecolor=bg, edgecolor=grid, labelcolor=txt)
ax4.grid(True, alpha=0.2, color=grid)
ax4.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))

# 5. Trade markers
ax5 = axes[2, 0]
ax5.plot(timestamps, balances, color=acc, linewidth=1.5, alpha=0.6)
if trades:
    t_times = [datetime.fromisoformat(t[0]) for t in trades]
    cum = 100
    cum_vals = []
    for t in trades:
        cum += t[1]
        cum_vals.append(cum)
    wt = [t_times[i] for i in range(len(trades)) if trades[i][2]]
    wv = [cum_vals[i] for i in range(len(trades)) if trades[i][2]]
    lt = [t_times[i] for i in range(len(trades)) if not trades[i][2]]
    lv = [cum_vals[i] for i in range(len(trades)) if not trades[i][2]]
    ax5.scatter(wt, wv, color=up, s=60, zorder=5, label=f'Wins ({len(wt)})', marker='^')
    ax5.scatter(lt, lv, color=dn, s=60, zorder=5, label=f'Losses ({len(lt)})', marker='v')
ax5.set_title('Trade Results on Balance', fontsize=14, fontweight='bold', color=txt)
ax5.set_ylabel('USDT', color=txt)
ax5.legend(loc='upper left', facecolor=bg, edgecolor=grid, labelcolor=txt)
ax5.grid(True, alpha=0.2, color=grid)
ax5.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))

# 6. Drawdown
ax6 = axes[2, 1]
ax6.plot(timestamps, drawdowns, color=dn, linewidth=2)
ax6.fill_between(timestamps, drawdowns, 0, alpha=0.3, color=dn)
ax6.set_title('Max Drawdown', fontsize=14, fontweight='bold', color=txt)
ax6.set_ylabel('%', color=txt)
ax6.grid(True, alpha=0.2, color=grid)
ax6.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))

plt.tight_layout(rect=[0, 0, 1, 0.95])
plt.savefig(OUTPUT, dpi=150, bbox_inches='tight', facecolor=bg)
print(f'OK: {OUTPUT}')
