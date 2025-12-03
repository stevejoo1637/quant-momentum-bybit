# ==========================================
# Quant Momentum v3.2R (Realistic Edition)
# Bybit Futures Auto Trading (2H)
# ==========================================

import os
import time
import ccxt
import numpy as np
import pandas as pd
from datetime import datetime

# ==============================
# 환경변수 (Render / VPS용)
# ==============================
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

# ==============================
# 거래소 객체 생성
# ==============================
exchange = ccxt.bybit({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "linear"}  # USDT Perpetual
})

# ==============================
# 파라미터 설정
# ==============================
TIMEFRAME = "2h"
SLIPPAGE = 0.0025
FEE = 0.001
STOP_LOSS = 0.015
TAKE_PROFIT1 = 0.025
LEVERAGE = 1
MAX_SLOTS = 4

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT"
]

# ==============================
# 보조 함수
# ==============================
def get_ohlcv(symbol):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=200)
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["rsi"] = ta_rsi(df["close"], 14)
    df["macd"], df["macd_signal"] = ta_macd(df["close"])
    df["atr20"], df["atr60"] = ta_atr(df["high"], df["low"], df["close"], 20), ta_atr(df["high"], df["low"], df["close"], 60)
    df["candle_score"] = ((df["close"] - df["open"]) / (df["high"] - df["low"] + 1e-9)) * 10
    return df

def ta_rsi(close, length=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(length).mean()
    avg_loss = loss.rolling(length).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def ta_macd(close):
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd, signal

def ta_atr(high, low, close, period=14):
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()

# ==============================
# 진입 신호 판단
# ==============================
def get_signal(df):
    last = df.iloc[-1]
    prev = df.iloc[-2]

    long_cond = (last["rsi"] < 40) and (prev["macd"] < prev["macd_signal"]) and (last["macd"] > last["macd_signal"]) and (last["candle_score"] >= 5)
    short_cond = (last["rsi"] > 60) and (prev["macd"] > prev["macd_signal"]) and (last["macd"] < last["macd_signal"]) and (last["candle_score"] <= -5)

    if long_cond:
        return "long"
    elif short_cond:
        return "short"
    else:
        return None

# ==============================
# 포지션 상태 확인
# ==============================
def get_position(symbol):
    positions = exchange.fetch_positions([symbol])
    for p in positions:
        if float(p["contracts"]) > 0:
            return p["side"].lower()
    return "none"

# ==============================
# 주문 실행
# ==============================
def execute_trade(symbol, signal):
    pos = get_position(symbol)
    balance = exchange.fetch_balance()
    usdt = balance["total"]["USDT"]
    price = exchange.fetch_ticker(symbol)["last"]
    size = (usdt / 4 / price)  # 슬롯당 25%

    if signal == "long" and pos != "long":
        if pos == "short":
            exchange.create_market_buy_order(symbol, size)
        exchange.create_market_buy_order(symbol, size)
        print(f"📈 Long Entry: {symbol}")

    elif signal == "short" and pos != "short":
        if pos == "long":
            exchange.create_market_sell_order(symbol, size)
        exchange.create_market_sell_order(symbol, size)
        print(f"📉 Short Entry: {symbol}")
    else:
        print(f"⏸️ Holding: {symbol}")

# ==============================
# 메인 루프
# ==============================
while True:
    try:
        print(f"\n=== {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC | Quant Momentum v3.2R ===")
        open_slots = 0

        for symbol in SYMBOLS:
            df = get_ohlcv(symbol)
            signal = get_signal(df)
            if signal:
                execute_trade(symbol, signal)
                open_slots += 1
                if open_slots >= MAX_SLOTS:
                    break

        print("✅ Cycle complete. Sleeping for 2 hours...\n")
        time.sleep(60 * 60 * 2)

    except Exception as e:
        print(f"⚠️ Error: {e}")
        time.sleep(60)
