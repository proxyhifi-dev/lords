import sqlite3
import json

conn = sqlite3.connect('data/runtime_state.db')
cursor = conn.execute('SELECT value FROM state WHERE key = ?', ('runtime',))
row = cursor.fetchone()
if row:
    state = json.loads(row[0])
    print('Database state:')
    print(f'  spot_price: {state.get("spot_price")}')
    print(f'  orb_high: {state.get("orb_high")}')
    print(f'  trading_enabled: {state.get("trading_enabled")}')
    print(f'  bot_running: {state.get("bot_running")}')
else:
    print('No state found in database')

conn.close()