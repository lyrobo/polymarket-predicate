import sqlite3
conn = sqlite3.connect("data/btc_predictor.db")
try:
    cursor = conn.execute("SELECT * FROM sim_portfolio")
    cols = [d[0] for d in cursor.description]
    print(f"Columns: {cols}")
    for row in cursor.fetchall():
        print(row)
except Exception as e:
    print(f"Error: {e}")
conn.close()
