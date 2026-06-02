"""
📊 Trade Journal — writes and reads detailed trade records.
Used by smart_bot.py and dashboard_server.py
"""

import json, time, sqlite3
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "trade_journal.db"


def init_db():
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            type TEXT NOT NULL,
            market TEXT,
            asset TEXT,
            side TEXT,
            price REAL,
            size REAL,
            cost REAL,
            pair_cost REAL,
            profit REAL,
            balance_before REAL,
            balance_after REAL,
            purpose TEXT,
            note TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS balance_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            balance REAL NOT NULL,
            locked_positions INTEGER,
            open_orders INTEGER,
            pairs_completed INTEGER,
            pair_pnl REAL
        )
    """)
    db.commit()
    return db


def log_trade(db: sqlite3.Connection, **kwargs):
    fields = ["ts", "type", "market", "asset", "side", "price", "size",
              "cost", "pair_cost", "profit", "balance_before", "balance_after",
              "purpose", "note"]
    values = [kwargs.get(f) for f in fields]
    db.execute(
        f"INSERT INTO trades ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
        values
    )
    db.commit()


def log_balance(db: sqlite3.Connection, balance: float, locked: int,
                orders: int, pairs: int, pair_pnl: float):
    db.execute(
        "INSERT INTO balance_log (ts,balance,locked_positions,open_orders,pairs_completed,pair_pnl) VALUES (?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), balance, locked, orders, pairs, pair_pnl)
    )
    db.commit()


def get_recent_trades(db: sqlite3.Connection, limit: int = 50) -> list:
    rows = db.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    cols = ["id","ts","type","market","asset","side","price","size",
            "cost","pair_cost","profit","balance_before","balance_after","purpose","note"]
    return [dict(zip(cols, row)) for row in rows]


def get_balance_history(db: sqlite3.Connection, limit: int = 100) -> list:
    rows = db.execute("SELECT * FROM balance_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    cols = ["id","ts","balance","locked_positions","open_orders","pairs_completed","pair_pnl"]
    return [dict(zip(cols, row)) for row in rows]


def get_summary(db: sqlite3.Connection) -> dict:
    pairs = db.execute("SELECT COUNT(*), COALESCE(SUM(profit),0) FROM trades WHERE type='pair'").fetchone()
    exits = db.execute("SELECT COUNT(*), COALESCE(SUM(profit),0) FROM trades WHERE type='exit'").fetchone()
    closed = db.execute("SELECT COUNT(*), COALESCE(SUM(profit),0) FROM trades WHERE type='position_close'").fetchone()
    fills = db.execute("SELECT COUNT(*) FROM trades WHERE type='single_fill'").fetchone()
    opens = db.execute("SELECT COUNT(*) FROM trades WHERE type='position_open'").fetchone()
    latest_bal = db.execute("SELECT balance FROM balance_log ORDER BY id DESC LIMIT 1").fetchone()
    total_closed = (pairs[0] or 0) + (exits[0] or 0) + (closed[0] or 0)
    total_closed_pnl = round((pairs[1] or 0) + (exits[1] or 0) + (closed[1] or 0), 2)
    open_positions = ((fills[0] or 0) - (exits[0] or 0) +
                      (opens[0] or 0) - (closed[0] or 0))
    return {
        "pair_count": pairs[0], "pair_pnl": round(pairs[1], 2),
        "closed_count": total_closed, "closed_pnl": total_closed_pnl,
        "open_positions": max(0, open_positions),
        "balance": round(latest_bal[0], 2) if latest_bal else 0,
        "net_pnl": total_closed_pnl,
    }
