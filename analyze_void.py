import sqlite3, json

conn = sqlite3.connect('/opt/btc-polymarket-predictor/data/btc_predictor.db')

# Today's breakdown
for s in ['void', 'cancelled', 'settled', 'pending', 'open']:
    cnt = conn.execute(
        "SELECT COUNT(*) FROM real_trades WHERE status=? AND timestamp > '2026-05-24'", 
        (s,)
    ).fetchone()[0]
    if cnt > 0:
        print(f"Today ({s}): {cnt}")

# All settled: direction breakdown
print("\nSettled by direction:")
for d in ['UP', 'DN', 'Down']:
    cnt = conn.execute("SELECT COUNT(*) FROM real_trades WHERE status='settled' AND direction=?", (d,)).fetchone()[0]
    if cnt > 0:
        print(f"  {d}: {cnt}")

# Latest settled
last = conn.execute("SELECT id, direction, pnl, timestamp FROM real_trades WHERE status='settled' ORDER BY id DESC LIMIT 3").fetchall()
print("\nLast settled:")
for r in last:
    print(f"  #{r[0]} {r[1]} PnL={r[2]:.2f} time={r[3]}")

# Check if the DB predates the new account
first = conn.execute("SELECT id, timestamp FROM real_trades ORDER BY id LIMIT 1").fetchone()
last_rec = conn.execute("SELECT id, timestamp FROM real_trades ORDER BY id DESC LIMIT 1").fetchone()
print(f"\nDB range: #{first[0]} ({first[1]}) to #{last_rec[0]} ({last_rec[1]})")

# Check for 'void' definition
void_sample = conn.execute("SELECT id, direction, size, pnl, timestamp FROM real_trades WHERE status='void' LIMIT 3").fetchall()
print("\nSample void records:")
for r in void_sample:
    print(f"  #{r[0]} dir={r[1]} size={r[2]} pnl={r[3]} time={r[4]}")

conn.close()
