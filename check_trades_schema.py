import sqlite3
conn = sqlite3.connect("data/btc_predictor.db")
cursor = conn.execute("PRAGMA table_info(sim_trades)")
for row in cursor.fetchall():
    print(row)
cursor = conn.execute("SELECT * FROM sim_trades ORDER BY id DESC LIMIT 3")
cols = [d[0] for d in cursor.description]
print(f"
Columns: {cols}")
for row in cursor.fetchall():
    print(row)
conn.close()
