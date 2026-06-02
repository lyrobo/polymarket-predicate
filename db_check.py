import sqlite3
conn = sqlite3.connect("data/btc_predictor.db")
# Check columns
cursor = conn.execute("PRAGMA table_info(realtime_predictions)")
cols = [row[1] for row in cursor.fetchall()]
print("Columns:", cols)
print("Has btc_price:", "btc_price" in cols)

# Check latest row
cursor = conn.execute("SELECT id, timestamp, btc_price, mid_price FROM realtime_predictions ORDER BY id DESC LIMIT 3")
for row in cursor.fetchall():
    print(f"id={row[0]}, btc_price={row[2]}, mid_price={row[3]}")

# Count null btc_price
cursor = conn.execute("SELECT COUNT(*) FROM realtime_predictions WHERE btc_price IS NULL")
print(f"Null btc_price: {cursor.fetchone()[0]}")
cursor = conn.execute("SELECT COUNT(*) FROM realtime_predictions WHERE btc_price IS NOT NULL")
print(f"Non-null btc_price: {cursor.fetchone()[0]}")
conn.close()
