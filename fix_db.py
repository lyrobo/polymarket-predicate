import sqlite3, shutil, os

DB = '/opt/btc-polymarket-predictor/data/btc_predictor.db'
BACKUP = '/opt/btc-polymarket-predictor/data/btc_predictor_backup.db'
CORRUPT = '/opt/btc-polymarket-predictor/data/btc_predictor_corrupt.db'

shutil.copy(DB, CORRUPT)
print(f'Corrupt backup saved to {CORRUPT}')

try:
    conn = sqlite3.connect(DB)
    conn.execute('SELECT COUNT(*) FROM real_trades').fetchone()
    print('DB readable - no corruption detected in current file')
    conn.close()
except Exception as e:
    print(f'DB corrupt: {e}')
    # Restore from backup
    if os.path.exists(BACKUP):
        shutil.copy(BACKUP, DB)
        print(f'Restored from backup ({os.path.getsize(BACKUP)} bytes)')
    else:
        print('No backup available')
        # Create fresh from schema in real_trader.py
        print('Need to recreate DB schema')
