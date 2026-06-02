import sqlite3
conn = sqlite3.connect("data/btc_predictor.db")
conn.execute("DELETE FROM sim_trades")
conn.execute("DELETE FROM sim_portfolio")
conn.commit()
print("Database reset: sim_trades and sim_portfolio cleared")
conn.close()
