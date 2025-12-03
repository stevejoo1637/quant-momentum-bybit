# ==========================================
# Quant Momentum v3.2R (Resilient Edition)
# Bybit Global API + Auto Retry + Proxy Support
# ==========================================

import os
import time
import ccxt
import numpy as np
import pandas as pd
import random
from datetime import datetime

# ──────────────────────────────────────────
# 환경 설정
# ──────────────────────────────────────────
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

# 프록시가 있다면 여기 설정 (없으면 None 유지)
PROXY = os.getenv("HTTP_PROXY") or None

exchange = ccxt.bybit({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "urls": {"api": "https://api.bybitglobal.com"},   # CloudFront 회피용 글로벌 엔드포인트
    "options": {"defaultType": "linear"},
    "proxies": {"http": PROXY, "https": PROXY} if PROXY else None
})

TIMEFRAME = "1m"
BASE_TP = 0.025
BASE_SL = 0.015
MAX_SLOTS = 4
LOG_FILE = "tradelog_v3.2R_resilient.csv"

SYMBOLS = ["BTC/USDT","ETH/USDT","SOL/USDT","XRP/USDT","BNB/USDT",
           "DOGE/USDT","ADA/USDT","AVAX/USDT","DOT/USDT","LINK/USDT"]

# ──────────────────────────────────────────
# 안전한 API 호출 (자동 재시도)
# ──────────────────────────────────────────
def safe_fetch(func, *args, retries=3, wait_range=(3, 8)):
    for i in range(retries):
        try:
            return func(*args)
        except Exception as e:
            print(f"⚠️ API Error ({i+1}/{retries}): {e}")
            sleep_time = random.randint(*wait_range)
            print(f"🔁 재시도 {sleep_time}초 후 재시도합니다...")
            time.sleep(sleep_time)
    raise Exception(f"❌ API 요청 실패 ({func.__name__}) after {retries} retries")

# ──────────────────────────────────────────
# 기술 지표 계산
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
# OHLCV 데이터 (안 끊기게 안전화)
# ──────────────────────────────────────────
def get_ohlcv(symbol):
    ohlcv = safe_fetch(exchange.fetch_ohlcv, symbol, TIMEFRAME, 200)
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
    positions = safe_fetch(exchange.fetch_positions)
    for p in positions:
        if p["symbol"] == symbol.replace("/", "") and float(p["contracts"]) > 0:
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
    balance = safe_fetch(exchange.fetch_balance)
    usdt = balance["total"]["USDT"]
    ticker = safe_fetch(exchange.fetch_ticker, symbol)
    price = ticker["last"]
    size = (usdt / 4 / price)

    try:
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

    except Exception as e:
        print(f"💥 주문 실패 ({symbol}): {e}")

# ──────────────────────────────────────────
# 트레이드 로그 저장
# ──────────────────────────────────────────
def log_trade(side, symbol, price, tp, sl):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    df = pd.DataFrame([{
        "time": now, "side": side, "symbol": symbol,
        "entry": price, "tp": tp, "sl": sl
    }])
    if not os.path.exists(LOG_FILE):
        df.to_csv(LOG_FILE, index=False)
    else:
        df.to_csv(LOG_FILE, mode='a', header=False, index=False)
    print(f"🧾 Logged {side} {symbol} @ {price:.2f}")

# ──────────────────────────────────────────
# 메인 루프
# ──────────────────────────────────────────
while True:
    try:
        print(f"\n[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] Quant Momentum v3.2R-Resilient running...")
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

    except Exception as e:
        print(f"⚠️ Critical error: {e}")
        print("🕒 90초 후 재시작 시도...")
        time.sleep(90)
