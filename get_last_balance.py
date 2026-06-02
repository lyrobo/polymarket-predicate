import sqlite3
conn = sqlite3.connect("data/btc_predictor.db")
cursor = conn.execute("SELECT id, wallet_balance_after FROM sim_trades ORDER BY id DESC LIMIT 1")
row = cursor.fetchone()
if row:
    print(f"Last trade id={row[0]}, wallet_balance_after=${row[1]:.2f}")
cursor = conn.execute("SELECT id, wallet_balance_after FROM sim_trades ORDER BY id ASC LIMIT 1")
row = cursor.fetchone()
if row:
    print(f"First trade id={row[0]}, wallet_balance_after=${row[1]:.2f}")
conn.close()
