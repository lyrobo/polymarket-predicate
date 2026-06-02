import sqlite3

conn = sqlite3.connect('/opt/btc-polymarket-predictor/data/btc_predictor.db')
conn.row_factory = sqlite3.Row

rows = conn.execute("SELECT * FROM state").fetchall()
print('Current state:')
for r in rows:
    print(f"  {r['key']}: {r['value']}")

conn.execute("UPDATE state SET value='0' WHERE key='peak_balance'")
conn.execute("UPDATE state SET value='0' WHERE key='start_balance'")
conn.commit()

rows2 = conn.execute("SELECT * FROM state WHERE key IN ('peak_balance','start_balance')").fetchall()
print('After reset:')
for r in rows2:
    print(f"  {r['key']}: {r['value']}")

conn.close()
print('Done')
