import sqlite3
conn = sqlite3.connect("data/btc_predictor.db")
cursor = conn.execute("SELECT id, timestamp, btc_price, mid_price FROM realtime_predictions ORDER BY id DESC LIMIT 3")
for row in cursor.fetchall():
    print(f"id={row[0]}, btc_price={row[2]}, mid_price={row[3]}")
conn.close()
