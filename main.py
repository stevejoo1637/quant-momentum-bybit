# ==========================================
# Quant Momentum v3.2R (Volatility Adaptive Full Auto)
# Bybit Futures 2H Strategy — Full Automation
# ==========================================

import os
import time
import ccxt
import numpy as np
import pandas as pd
from datetime import datetime

# ──────────────────────────────────────────
# 환경 설정
# ──────────────────────────────────────────
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

exchange = ccxt.bybit({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "linear"}  # USDT Perpetual
})

TIMEFRAME = "2h"
BASE_TP = 0.025
BASE_SL = 0.015
MAX_SLOTS = 4
LEVERAGE = 1
LOG_FILE = "tradelog_v3.2R.csv"

SYMBOLS = ["BTC/USDT","ETH/USDT","SOL/USDT","XRP/USDT","BNB/USDT",
           "DOGE/USDT","ADA/USDT","AVAX/USDT","DOT/USDT","LINK/USDT"]

# ──────────────────────────────────────────
# 기술지표 함수
# ──────────────────────────────────────────
def ta_rsi(close, length=14):
    delta = close.diff()
    gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
    avg_gain, avg_loss = gain.rolling(length).mean(), loss.rolling(length).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def ta_macd(close):
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd, signal = ema12 - ema26, (ema12 - ema26).ewm(span=9, adjust=False).mean()
    return macd, signal

def ta_atr(high, low, close, period=14):
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def ta_ma(close, period=20):
    return close.rolling(period).mean()

# ──────────────────────────────────────────
# 데이터 로딩
# ──────────────────────────────────────────
def get_ohlcv(symbol):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=200)
    df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
    df["rsi"] = ta_rsi(df["close"], 14)
    df["macd"], df["macd_signal"] = ta_macd(df["close"])
    df["atr20"], df["atr60"] = ta_atr(df["high"], df["low"], df["close"], 20), ta_atr(df["high"], df["low"], df["close"], 60)
    df["ma20"] = ta_ma(df["close"], 20)
    df["candle_score"] = ((df["close"] - df["open"]) / (df["high"] - df["low"] + 1e-9)) * 10
    return df.dropna().reset_index(drop=True)

# ──────────────────────────────────────────
# 신호 판정
# ──────────────────────────────────────────
def get_signal(df):
    last, prev = df.iloc[-1], df.iloc[-2]
    long_cond  = (last["rsi"] < 40) and (prev["macd"] < prev["macd_signal"]) and (last["macd"] > last["macd_signal"]) and (last["candle_score"] >= 5)
    short_cond = (last["rsi"] > 60) and (prev["macd"] > prev["macd_signal"]) and (last["macd"] < last["macd_signal"]) and (last["candle_score"] <= -5)
    if long_cond: return "long"
    if short_cond: return "short"
    return None

# ──────────────────────────────────────────
# 포지션 조회
# ──────────────────────────────────────────
def get_position(symbol):
    positions = exchange.fetch_positions()
    for p in positions:
        if p["symbol"] == symbol.replace("/", ""):
            if float(p["contracts"]) > 0:
                return p["side"].lower()
    return "none"

# ──────────────────────────────────────────
# Volatility Adaptive TP/SL 계산
# ──────────────────────────────────────────
def volatility_scaled_tp_sl(df_i, df_btc):
    ratio = (df_i["atr20"].iloc[-1] / df_btc["atr20"].iloc[-1]) ** 0.6
    tp = BASE_TP * ratio
    sl = BASE_SL * ratio
    return tp, sl

# ──────────────────────────────────────────
# 주문 실행
# ──────────────────────────────────────────
def execute_trade(symbol, signal, tp_rate, sl_rate):
    pos = get_position(symbol)
    balance = exchange.fetch_balance()
    usdt = balance["total"]["USDT"]
    price = exchange.fetch_ticker(symbol)["last"]
    size = (usdt / 4 / price)  # 4슬롯 배분

    if signal == "long" and pos != "long":
        tp = price * (1 + tp_rate)
        sl = price * (1 - sl_rate)
        exchange.create_market_buy_order(symbol, size)
        exchange.create_order(symbol, "take_profit_market", "sell", size * 0.4, None, {"stopPrice": tp})
        exchange.create_order(symbol, "stop_market", "sell", size, None, {"stopPrice": sl})
        log_trade("LONG", symbol, price, tp, sl)

    elif signal == "short" and pos != "short":
        tp = price * (1 - tp_rate)
        sl = price * (1 + sl_rate)
        exchange.create_market_sell_order(symbol, size)
        exchange.create_order(symbol, "take_profit_market", "buy", size * 0.4, None, {"stopPrice": tp})
        exchange.create_order(symbol, "stop_market", "buy", size, None, {"stopPrice": sl})
        log_trade("SHORT", symbol, price, tp, sl)

# ──────────────────────────────────────────
# 트레이드 로그
# ──────────────────────────────────────────
def log_trade(side, symbol, price, tp, sl):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    entry = {"time": now, "side": side, "symbol": symbol, "entry": price, "tp": tp, "sl": sl}
    df = pd.DataFrame([entry])
    if not os.path.exists(LOG_FILE):
        df.to_csv(LOG_FILE, index=False)
    else:
        df.to_csv(LOG_FILE, mode='a', header=False, index=False)
    print(f"🧾 Logged trade {side} {symbol} | Entry {price:.2f}")

# ──────────────────────────────────────────
# 메인 루프
# ──────────────────────────────────────────
while True:
    try:
        print(f"\n[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] Quant Momentum v3.2R FULL AUTO running...")
        open_slots = 0
        df_btc = get_ohlcv("BTC/USDT")

        for symbol in SYMBOLS:
            if open_slots >= MAX_SLOTS:
                break

            df_i = get_ohlcv(symbol)
            signal = get_signal(df_i)
            if signal:
                tp_rate, sl_rate = volatility_scaled_tp_sl(df_i, df_btc)
                execute_trade(symbol, signal, tp_rate, sl_rate)
                open_slots += 1

        print("✅ Cycle complete. Sleeping 2h...\n")
        time.sleep(60 * 60 * 2)

    except ccxt.NetworkError as e:
        print(f"🌐 Network Error: {e}")
        time.sleep(30)
    except ccxt.ExchangeError as e:
        print(f"💥 Exchange Error: {e}")
        time.sleep(60)
    except Exception as e:
        print(f"⚠️ Unexpected Error: {e}")
        time.sleep(90)
