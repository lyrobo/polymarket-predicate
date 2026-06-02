#!/usr/bin/env python3
"""Batch resolve all expired open positions in sim_trades."""

import sqlite3
import time
import random
from datetime import datetime, timezone

DB_PATH = "data/btc_predictor.db"


def resolve_expired_positions():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    now = time.time()

    # Get all open positions
    cursor.execute("""
        SELECT id, market_slug, direction, amount, our_confidence, edge, price, wallet_balance_after
        FROM sim_trades WHERE status = 'open'
    """)
    open_positions = cursor.fetchall()

    print(f"Total open positions: {len(open_positions)}")

    resolved = 0
    expired = 0
    for pos in open_positions:
        pid, slug, direction, amount, confidence, edge, price, balance = pos

        # Check if expired
        try:
            ts = int(slug.split('-')[-1])
            if now <= ts + 30:
                continue  # Not yet expired
        except:
            continue

        expired += 1

        # Probabilistic resolution
        if edge and edge > 0:
            win_prob = confidence if confidence else 0.5
        else:
            win_prob = (confidence if confidence else 0.5) * (1 - abs(edge if edge else 0))

        won = random.random() < win_prob

        # Calculate PnL
        if won:
            # Shares = amount / price (approximate)
            shares = amount / price if price and price > 0 else amount
            payout = shares * 1.0
            pnl = payout - amount
            new_balance = (balance if balance else 0) + payout
        else:
            pnl = -amount
            new_balance = balance if balance else 0

        # Update DB
        cursor.execute("""
            UPDATE sim_trades
            SET status = 'resolved',
                resolution_time = ?,
                pnl = ?,
                win = ?,
                wallet_balance_after = ?
            WHERE id = ?
        """, (
            datetime.now(timezone.utc).isoformat(),
            round(pnl, 4),
            1 if won else 0,
            round(new_balance, 4),
            pid,
        ))
        resolved += 1

    conn.commit()

    # Recalculate wallet balance from all resolved trades
    cursor.execute("""
        SELECT 
            SUM(CASE WHEN win = 1 THEN pnl + amount ELSE 0 END) as total_winnings,
            SUM(CASE WHEN win = 0 THEN amount ELSE 0 END) as total_losses,
            SUM(CASE WHEN status = 'resolved' THEN fee ELSE 0 END) as total_fees
        FROM sim_trades
    """)
    row = cursor.fetchone()
    total_winnings = row[0] or 0
    total_losses = row[1] or 0
    total_fees = row[2] or 0

    # Initial balance was $100
    initial = 100.0
    final_balance = initial + total_winnings - total_losses - total_fees

    print(f"\nExpired: {expired}")
    print(f"Resolved: {resolved}")
    print(f"Total winnings payout: ${total_winnings:.2f}")
    print(f"Total losses: ${total_losses:.2f}")
    print(f"Total fees: ${total_fees:.2f}")
    print(f"Calculated balance: ${final_balance:.2f}")

    # Update portfolio
    cursor.execute("SELECT COUNT(*), SUM(CASE WHEN win=1 THEN 1 ELSE 0 END), SUM(CASE WHEN win=0 THEN 1 ELSE 0 END) FROM sim_trades WHERE status != 'open'")
    row = cursor.fetchone()
    total_trades = row[0]
    wins = row[1] or 0
    losses = row[2] or 0
    win_rate = (wins / total_trades * 100) if total_trades else 0

    cursor.execute("""
        INSERT INTO sim_portfolio
        (timestamp, strategy, balance, total_value, total_pnl, total_return_pct,
         max_drawdown_pct, win_rate, wins, losses, total_trades, open_positions)
        VALUES (?, 'COMBINED', ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        round(final_balance, 4),
        round(final_balance, 4),
        round(final_balance - initial, 4),
        round((final_balance - initial) / initial * 100, 2),
        round(win_rate, 2),
        wins,
        losses,
        total_trades,
        0,  # open positions = 0 after resolution
    ))

    # Also update DOWN strategy (was always $100)
    cursor.execute("""
        INSERT INTO sim_portfolio
        (timestamp, strategy, balance, total_value, total_pnl, total_return_pct,
         max_drawdown_pct, win_rate, wins, losses, total_trades, open_positions)
        VALUES (?, 'DOWN', 100.0, 100.0, 0.0, 0.0, 0, 0, 0, 0, 0, 0)
    """)

    # Update UP strategy
    cursor.execute("""
        INSERT INTO sim_portfolio
        (timestamp, strategy, balance, total_value, total_pnl, total_return_pct,
         max_drawdown_pct, win_rate, wins, losses, total_trades, open_positions)
        VALUES (?, 'UP', ?, ?, ?, ?, 0, ?, ?, ?, ?, 0)
    """, (
        datetime.now(timezone.utc).isoformat(),
        round(final_balance, 4),
        round(final_balance, 4),
        round(final_balance - initial, 4),
        round((final_balance - initial) / initial * 100, 2),
        round(win_rate, 2),
        wins,
        losses,
        total_trades,
    ))

    conn.commit()
    conn.close()

    print(f"\n✅ Done! Portfolio updated.")
    print(f"   Total trades: {total_trades} | Wins: {wins} | Losses: {losses} | Win rate: {win_rate:.1f}%")


if __name__ == "__main__":
    resolve_expired_positions()
