"""SQLite DB 로거 - bybit_main.py에서 import하여 사용"""
import sqlite3
from datetime import datetime, timezone

DB_PATH = "/root/bybit_strategy/trading.db"

def _conn():
    return sqlite3.connect(DB_PATH)

def log_trade(symbol, side, entry_price, exit_price, qty, pnl, pnl_pct,
              fees=0, strategy="", reason="", hold_days=0):
    conn = _conn()
    conn.execute(
        """INSERT INTO trades (timestamp, exchange, symbol, side, entry_price,
           exit_price, qty, pnl, pnl_pct, fees, strategy, reason, hold_days)
           VALUES (?, 'bybit', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(), symbol, side, entry_price,
         exit_price, qty, pnl, pnl_pct, fees, strategy, reason, hold_days)
    )
    conn.commit()
    conn.close()

def upsert_position(symbol, side, entry_price, qty, sl_price=None,
                     tp_price=None, strategy="", entry_time=""):
    conn = _conn()
    conn.execute(
        """INSERT INTO positions (exchange, symbol, side, entry_price, qty,
           sl_price, tp_price, strategy, entry_time, updated_at)
           VALUES ('bybit', ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(exchange, symbol) DO UPDATE SET
           side=excluded.side, entry_price=excluded.entry_price, qty=excluded.qty,
           sl_price=excluded.sl_price, tp_price=excluded.tp_price,
           strategy=excluded.strategy, updated_at=datetime('now')""",
        (symbol, side, entry_price, qty, sl_price, tp_price, strategy, entry_time)
    )
    conn.commit()
    conn.close()

def remove_position(symbol):
    conn = _conn()
    conn.execute("DELETE FROM positions WHERE exchange='bybit' AND symbol=?", (symbol,))
    conn.commit()
    conn.close()

def log_daily(date_str, equity, daily_pnl, daily_pnl_pct, open_positions,
              total_trades, win_trades, btc_price, btc_state):
    conn = _conn()
    conn.execute(
        """INSERT INTO daily_performance (date, exchange, equity, daily_pnl,
           daily_pnl_pct, open_positions, total_trades, win_trades,
           btc_price, btc_market_state)
           VALUES (?, 'bybit', ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(date, exchange) DO UPDATE SET
           equity=excluded.equity, daily_pnl=excluded.daily_pnl,
           daily_pnl_pct=excluded.daily_pnl_pct,
           open_positions=excluded.open_positions,
           total_trades=excluded.total_trades, win_trades=excluded.win_trades,
           btc_price=excluded.btc_price, btc_market_state=excluded.btc_market_state""",
        (date_str, equity, daily_pnl, daily_pnl_pct, open_positions,
         total_trades, win_trades, btc_price, btc_state)
    )
    conn.commit()
    conn.close()

def log_alert(level, source, message):
    conn = _conn()
    conn.execute(
        "INSERT INTO alert_log (level, source, message, sent) VALUES (?, ?, ?, 1)",
        (level, source, message)
    )
    conn.commit()
    conn.close()
