
import sqlite3
DB_PATH = "/root/bybit_strategy/trading.db"
conn = sqlite3.connect(DB_PATH)
conn.executescript('''
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    exchange TEXT DEFAULT 'bybit',
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL,
    exit_price REAL,
    qty REAL,
    pnl REAL,
    pnl_pct REAL,
    fees REAL DEFAULT 0,
    strategy TEXT,
    reason TEXT,
    hold_days INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT DEFAULT 'bybit',
    symbol TEXT NOT NULL,
    side TEXT,
    entry_price REAL,
    qty REAL,
    sl_price REAL,
    tp_price REAL,
    strategy TEXT,
    entry_time TEXT,
    updated_at TEXT,
    UNIQUE(exchange, symbol)
);
CREATE TABLE IF NOT EXISTS daily_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    exchange TEXT DEFAULT 'bybit',
    equity REAL,
    daily_pnl REAL,
    daily_pnl_pct REAL,
    open_positions INTEGER,
    total_trades INTEGER,
    win_trades INTEGER,
    btc_price REAL,
    btc_market_state TEXT,
    UNIQUE(date, exchange)
);
CREATE TABLE IF NOT EXISTS alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    level TEXT,
    source TEXT,
    message TEXT,
    sent INTEGER DEFAULT 0
);
''')
cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cursor.fetchall()
print(f"Tables: {[t[0] for t in tables]}")
conn.close()
