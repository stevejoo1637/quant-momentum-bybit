# ==========================================
# Quant Momentum v3.3R - Render Safe Edition
# ------------------------------------------
# Render 환경 최적화 (안 끊김 / 실시간 신호 출력)
# ==========================================

import os
import time
import random
import ccxt
import pandas as pd
from datetime import datetime

# ---- Bybit API 연결 ----
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

exchange = ccxt.bybit({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "timeout": 10000,
    "rateLimit": 2000,
    "options": {"defaultType": "linear"},
    "urls": {"api": "https://api.bybitglobal.com"}
})

# ---- 기본 설정 ----
TIMEFRAME = "1m"
LEVERAGE = 3
BASE_TP = 0.025
BASE_SL = 0.015
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
MAX_SLOTS = 4

# ==========================================
# 지표 계산 함수
# ==========================================
def ta_rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(n).mean()
    avg_loss = loss.rolling(n).mean()
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

# ==========================================
# 안전한 API 호출 (재시도)
# ==========================================
def safe_fetch(func, *args, retries=3, wait=(2,5)):
    for i in range(retries):
        try:
            return func(*args)
        except Exception as e:
            print(f"⚠️ API 오류 {func.__name__} ({i+1}/{retries}) → {e}")
            time.sleep(random.randint(*wait))
    print(f"❌ {func.__name__} 3회 실패")
    return None

# ==========================================
# OHLCV 데이터 불러오기
# ==========================================
def get_ohlcv(symbol):
    data = safe_fetch(exchange.fetch_ohlcv, symbol, TIMEFRAME, 200)
    if not data:
        print(f"⚠️ {symbol} 데이터 불러오기 실패")
        return None
    df = pd.DataFrame(data, columns=["ts","open","high","low","close","volume"])
    df["rsi"] = ta_rsi(df["close"])
    df["macd"], df["macd_signal"] = ta_macd(df["close"])
    df["atr20"] = ta_atr(df["high"], df["low"], df["close"], 20)
    df["candle_score"] = ((df["close"] - df["open"]) / (df["high"] - df["low"] + 1e-9)) * 10
    return df.dropna()

# ==========================================
# 진입 신호 감지
# ==========================================
def get_signal(df):
    last, prev = df.iloc[-1], df.iloc[-2]
    long_cond = (
        last["rsi"] < 40
        and prev["macd"] < prev["macd_signal"]
        and last["macd"] > last["macd_signal"]
        and last["candle_score"] >= 5
    )
    short_cond = (
        last["rsi"] > 60
        and prev["macd"] > prev["macd_signal"]
        and last["macd"] < last["macd_signal"]
        and last["candle_score"] <= -5
    )
    if long_cond:
        return "LONG"
    elif short_cond:
        return "SHORT"
    return None

# ==========================================
# 주문 실행 (빈 함수 → 직접 추가 가능)
# ==========================================
def execute_trade(symbol, signal, price):
    """
    여기에 실제 주문 코드를 직접 추가하면 완전 자동화됩니다.
    예시 구조:
        exchange.create_market_order(
            symbol=symbol,
            side="buy" if signal == "LONG" else "sell",
            amount=size,
            params={"takeProfitPrice": tp, "stopLossPrice": sl}
        )
    """
    print(f"🚀 [{signal}] {symbol} | 가격: {price:.2f}")

# ==========================================
# 메인 루프
# ==========================================
print(f"🚀 Quant Momentum v3.3R 시작됨 ({datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')})")

loop = 0
while True:
    loop += 1
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n💓 Loop #{loop} | UTC {now}")

    try:
        for sym in SYMBOLS:
            df = get_ohlcv(sym)
            if df is None:
                continue

            signal = get_signal(df)
            price = df["close"].iloc[-1]
            rsi = df["rsi"].iloc[-1]

            if signal:
                execute_trade(sym, signal, price)
            else:
                print(f"⚪ {sym} | RSI={rsi:.1f} | 신호 없음")

        print(f"✅ Loop 완료 | 60초 대기...\n")
        time.sleep(60)

    except Exception as e:
        print(f"💥 메인 루프 오류: {e}")
        time.sleep(15)
