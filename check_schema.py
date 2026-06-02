import sqlite3
conn = sqlite3.connect("data/btc_predictor.db")
cursor = conn.execute("PRAGMA table_info(realtime_predictions)")
for row in cursor.fetchall():
    print(row)
conn.close()
