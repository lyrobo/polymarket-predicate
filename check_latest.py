import sqlite3
conn = sqlite3.connect("data/btc_predictor.db")
cursor = conn.execute("SELECT * FROM realtime_predictions ORDER BY id DESC LIMIT 1")
cols = [d[0] for d in cursor.description]
print("Columns:", cols)
row = cursor.fetchone()
if row:
    for c, v in zip(cols, row):
        print(f"  {c}: {v}")
conn.close()
