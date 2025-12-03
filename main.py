# ==========================================
# Quant Momentum v3.2R - Starter Stable Edition
# ------------------------------------------
# Render Starter (1m, continuous running)
# ==========================================

import os
import time
import random
import ccxt
import pandas as pd
from datetime import datetime

# ---- Bybit API ----
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

exchange = ccxt.bybit({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "timeout": 10000,
    "rateLimit": 2000,
    "options": {"defaultType": "linear"},
    "urls": {"api": "https://api.bybitglobal.com"}   # ✅ 문법 오류 수정됨
})

# ---- Settings ----
TIMEFRAME = "1m"
BASE_TP = 0.025
BASE_SL = 0.015
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

# ---- Helper Functions ----
def ta_rsi(close, n=14):
    delta = close.diff()
    gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
    avg_gain, avg_loss = gain.rolling(n).mean(), loss.rolling(n).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def ta_macd(close):
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd, signal

def ta_atr(high, low, close, n=14):
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def safe_fetch(func, *args, retries=3, wait=(2,5)):
    """안정적 API 호출 - 오류시 자동 재시도"""
    for i in range(retries):
        try:
            return func(*args)
        except Exception as e:
            print(f"⚠️ [{func.__name__}] 실패 ({i+1}/{retries}) - {e}")
            time.sleep(random.randint(*wait))
    print(f"❌ [{func.__name__}] 3회 재시도 실패")
    return None

# ---- Data Fetch ----
def get_ohlcv(symbol):
    data = safe_fetch(exchange.fetch_ohlcv, symbol, TIMEFRAME, 200)
    if not data:
        print(f"⚠️ {symbol} 데이터 없음")
        return None
    df = pd.DataFrame(data, columns=["ts","open","high","low","close","volume"])
    df["rsi"] = ta_rsi(df["close"])
    df["macd"], df["macd_signal"] = ta_macd(df["close"])
    df["atr20"] = ta_atr(df["high"], df["low"], df["close"], 20)
    df["candle_score"] = ((df["close"] - df["open"]) / 
                          (df["high"] - df["low"] + 1e-9)) * 10
    return df.dropna()

def get_signal(df):
    """진입 신호 탐지"""
    last, prev = df.iloc[-1], df.iloc[-2]
    long_cond  = (last["rsi"] < 40) and (prev["macd"] < prev["macd_signal"]) \
                 and (last["macd"] > last["macd_signal"]) and (last["candle_score"] >= 5)
    short_cond = (last["rsi"] > 60) and (prev["macd"] > prev["macd_signal"]) \
                 and (last["macd"] < last["macd_signal"]) and (last["candle_score"] <= -5)
    if long_cond: return "LONG"
    if short_cond: return "SHORT"
    return "NONE"

# ---- Main Loop ----
loop = 0
print(f"🚀 Quant Momentum v3.2R Starter Edition Initialized ({datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')})")

while True:
    loop += 1
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n💓 Alive | UTC {now} | Loop #{loop}")

    try:
        for sym in SYMBOLS:
            df = get_ohlcv(sym)
            if df is None: 
                continue
            signal = get_signal(df)
            rsi = df["rsi"].iloc[-1]
            macd = df["macd"].iloc[-1]
            if signal != "NONE":
                print(f"📈 {sym} → {signal} | RSI={rsi:.1f} | MACD={macd:.5f}")
            else:
                print(f"⚪ {sym} | RSI={rsi:.1f} | MACD={macd:.5f} | No signal")
        
        print(f"✅ Loop #{loop} complete. Sleeping 60s...\n")
        time.sleep(60)

    except Exception as e:
        print(f"💥 Main Loop Error: {e}")
        time.sleep(15)
