import sqlite3
conn = sqlite3.connect("data/btc_predictor.db")
try:
    conn.execute("ALTER TABLE realtime_predictions ADD COLUMN btc_price REAL")
    print("Added btc_price column")
except:
    print("btc_price column already exists")
# Copy mid_price to btc_price
cursor = conn.execute("UPDATE realtime_predictions SET btc_price=mid_price WHERE btc_price IS NULL AND mid_price>0")
conn.commit()
print(f"Updated {cursor.rowcount} rows")
conn.close()
