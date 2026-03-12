import sqlite3

conn = sqlite3.connect("/root/bybit_strategy/trading.db")
c = conn.cursor()

c.execute("""CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
    exchange TEXT NOT NULL DEFAULT 'bybit', symbol TEXT NOT NULL,
    side TEXT NOT NULL, entry_price REAL, exit_price REAL, qty REAL,
    pnl REAL, pnl_pct REAL, fees REAL DEFAULT 0, strategy TEXT,
    reason TEXT, hold_days INTEGER, created_at TEXT DEFAULT (datetime('now'))
)""")

c.execute("""CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, exchange TEXT NOT NULL DEFAULT 'bybit',
    symbol TEXT NOT NULL, side TEXT NOT NULL, entry_price REAL NOT NULL,
    qty REAL NOT NULL, sl_price REAL, tp_price REAL, strategy TEXT,
    entry_time TEXT NOT NULL, updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(exchange, symbol)
)""")

c.execute("""CREATE TABLE IF NOT EXISTS daily_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL,
    exchange TEXT NOT NULL DEFAULT 'bybit', equity REAL, daily_pnl REAL,
    daily_pnl_pct REAL, open_positions INTEGER DEFAULT 0,
    total_trades INTEGER DEFAULT 0, win_trades INTEGER DEFAULT 0,
    btc_price REAL, btc_market_state TEXT,
    created_at TEXT DEFAULT (datetime('now')), UNIQUE(date, exchange)
)""")

c.execute("""CREATE TABLE IF NOT EXISTS alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    level TEXT NOT NULL, source TEXT, message TEXT NOT NULL, sent INTEGER DEFAULT 0
)""")

c.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)")
c.execute("CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp)")
c.execute("CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_performance(date)")

conn.commit()
tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
for t in tables:
    print(t[0])
conn.close()
print("VPS DB OK")
