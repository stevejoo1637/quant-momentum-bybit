# ==========================================
# Quant Momentum v3.2R - Final Stable Edition
# Bybit USDT Perpetual Auto-Trading (2H)
# ==========================================

import os
import time
import random
import pandas as pd
import ccxt
from datetime import datetime

# ------------------------------------------
# API Key 환경 변수 (Render Secrets에 저장)
# ------------------------------------------
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

# ------------------------------------------
# Bybit Exchange 설정 (문법 오류 수정된 버전)
# ------------------------------------------
exchange = ccxt.bybit({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "timeout": 10000,  # ⏱️ 10초 이상 응답 없으면 자동 실패 처리
    "urls": {
        "api": "https://api.bybitglobal.com"  # ✅ 글로벌 엔드포인트 (CloudFront 차단 회피)
    },
    "options": {
        "defaultType": "linear"  # ✅ USDT Perpetual
    }
})

# ------------------------------------------
# 전략 파라미터
# ------------------------------------------
TIMEFRAME = "1m"
BASE_TP = 0.025
BASE_SL = 0.015
MAX_SLOTS = 4
LOG_FILE = "tradelog_v3.2R_final.csv"

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
    "ADA/USDT", "AVAX/USDT", "DOGE/USDT", "LINK/USDT", "TRX/USDT"
]

# ------------------------------------------
# 기술지표 함수
# ------------------------------------------
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

# ------------------------------------------
# 안전한 API 호출 (타임아웃 + 재시도 포함)
# ------------------------------------------
def safe_fetch(func, *args, retries=3, wait=(3, 6)):
    for i in range(retries):
        try:
            return func(*args)
        except Exception as e:
            print(f"⚠️ API Error ({i+1}/{retries}): {e}")
            time.sleep(random.randint(*wait))
    raise Exception(f"❌ {func.__name__} failed after {retries} retries")

# ------------------------------------------
# OHLCV 데이터 가져오기
# ------------------------------------------
def get_ohlcv(symbol):
    print(f"🕒 Fetching OHLCV for {symbol} ...")
    data = safe_fetch(exchange.fetch_ohlcv, symbol, TIMEFRAME, 200)
    df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "volume"])
    df["rsi"] = ta_rsi(df["close"])
    df["macd"], df["macd_signal"] = ta_macd(df["close"])
    df["atr20"] = ta_atr(df["high"], df["low"], df["close"], 20)
    df["atr60"] = ta_atr(df["high"], df["low"], df["close"], 60)
    df["candle_score"] = ((df["close"] - df["open"]) /
                          (df["high"] - df["low"] + 1e-9)) * 10
    return df.dropna()

# ------------------------------------------
# 신호 판정 (RSI + MACD + Candle Score)
# ------------------------------------------
def get_signal(df):
    last, prev = df.iloc[-1], df.iloc[-2]
    long_cond = (last["rsi"] < 40) and (prev["macd"] < prev["macd_signal"]) \
                and (last["macd"] > last["macd_signal"]) and (last["candle_score"] >= 5)
    short_cond = (last["rsi"] > 60) and (prev["macd"] > prev["macd_signal"]) \
                 and (last["macd"] < last["macd_signal"]) and (last["candle_score"] <= -5)
    if long_cond:
        return "long"
    if short_cond:
        return "short"
    return None

# ------------------------------------------
# 거래 실행
# ------------------------------------------
def execute_trade(symbol, signal):
    try:
        balance = safe_fetch(exchange.fetch_balance)
        usdt = balance["total"]["USDT"]
        price = safe_fetch(exchange.fetch_ticker, symbol)["last"]
        size = usdt / 4 / price  # 슬롯당 25%
        side = "buy" if signal == "long" else "sell"

        tp = price * (1 + BASE_TP) if signal == "long" else price * (1 - BASE_TP)
        sl = price * (1 - BASE_SL) if signal == "long" else price * (1 + BASE_SL)

        order = safe_fetch(exchange.create_market_order, symbol, side, size)
        print(f"📈 {signal.upper()} {symbol} | Entry {price:.2f} | TP {tp:.2f} | SL {sl:.2f}")
        log_trade(signal, symbol, price, tp, sl)
    except Exception as e:
        print(f"💥 Trade error on {symbol}: {e}")

# ------------------------------------------
# 거래 로그 기록
# ------------------------------------------
def log_trade(side, symbol, price, tp, sl):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    entry = pd.DataFrame([{
        "time": now,
        "side": side,
        "symbol": symbol,
        "entry": price,
        "tp": tp,
        "sl": sl
    }])
    if not os.path.exists(LOG_FILE):
        entry.to_csv(LOG_FILE, index=False)
    else:
        entry.to_csv(LOG_FILE, mode='a', header=False, index=False)
    print(f"🧾 Logged {side} {symbol} @ {price:.2f}")

# ------------------------------------------
# 메인 루프
# ------------------------------------------
while True:
    print(f"\n[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] 🚀 Quant Momentum v3.2R-Final running...")
    try:
        for symbol in SYMBOLS:
            df = get_ohlcv(symbol)
            signal = get_signal(df)
            if signal:
                execute_trade(symbol, signal)
        print("✅ Cycle complete. Sleeping 2h...\n")
        time.sleep(60 * 60 * 2)
    except Exception as e:
        print(f"⚠️ Main loop error: {e}")
        time.sleep(60)
