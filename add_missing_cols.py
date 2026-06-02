import sqlite3
conn = sqlite3.connect("data/btc_predictor.db")
columns_to_add = [
    ("up_price", "REAL"),
    ("down_price", "REAL"),
    ("edge", "REAL"),
    ("our_confidence", "REAL"),
    ("market_slug", "TEXT"),
    ("market_question", "TEXT"),
]
for col_name, col_type in columns_to_add:
    try:
        conn.execute(f"ALTER TABLE realtime_predictions ADD COLUMN {col_name} {col_type}")
        print(f"Added {col_name}")
    except Exception as e:
        if "duplicate column" in str(e).lower():
            print(f"{col_name} already exists")
        else:
            print(f"Error adding {col_name}: {e}")
conn.commit()
conn.close()
print("Done!")
