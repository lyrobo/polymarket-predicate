import sqlite3
conn = sqlite3.connect("data/btc_predictor.db")
cursor = conn.execute("SELECT * FROM sim_portfolio ORDER BY id DESC LIMIT 5")
cols = [d[0] for d in cursor.description]
print(f"Columns: {cols}")
for row in cursor.fetchall():
    for c, v in zip(cols, row):
        print(f"  {c}: {v}")
    print("---")
conn.close()
