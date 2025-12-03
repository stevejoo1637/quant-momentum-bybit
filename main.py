import ccxt
import pandas as pd
import numpy as np
import time
import ta
import requests
import os

# ===== API 키 불러오기 =====
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

# ===== 거래 설정 =====
symbol = "BTC/USDT"
timeframe = "2h"
leverage = 1
allocation = 0.25  # 전체 자산의 25%만 사용
exchange = ccxt.bybit({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
})
exchange.set_leverage(leverage, symbol)

# ===== RSI, MACD, ATR 계산 =====
def get_indicators(df):
    df["rsi"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    macd = ta.trend.MACD(df["close"])
    df["macd"] = macd.macd()
    df["signal"] = macd.macd_signal()
    df["atr"] = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=20).average_true_range()
    return df

# ===== 시세 데이터 불러오기 =====
def fetch_ohlcv():
    bars = exchange.fetch_ohlcv(symbol, timeframe, limit=200)
    df = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return get_indicators(df)

# ===== 포지션 확인 =====
def get_position():
    balance = exchange.fetch_balance(params={"type": "future"})
    pos = balance["info"]["result"]
    for p in pos:
        if p["data"]["symbol"] == "BTCUSDT":
            return p
    return None

# ===== 매수 / 매도 =====
def place_order(side, size):
    print(f"💥 {side.upper()} {size} BTC/USDT")
    order = exchange.create_market_order(symbol, side, size)
    print(order)
    return order

# ===== 메인 전략 =====
def strategy():
    df = fetch_ohlcv()
    rsi = df["rsi"].iloc[-1]
    macd = df["macd"].iloc[-1]
    signal = df["signal"].iloc[-1]

    balance = exchange.fetch_balance()
    usdt = balance["total"]["USDT"]
    price = df["close"].iloc[-1]
    size = (usdt * allocation) / price

    pos = get_position()

    # 진입 조건
    if rsi < 40 and macd > signal:
        if pos is None or pos["data"]["side"] != "Buy":
            place_order("buy", size)
    elif rsi > 60 and macd < signal:
        if pos is None or pos["data"]["side"] != "Sell":
            place_order("sell", size)
    else:
        print("⚖️ No clear signal")

# ===== 루프 실행 =====
print("🚀 Quant Momentum Bot Started")
while True:
    try:
        strategy()
        print("⏳ Waiting for next candle...\n")
        time.sleep(60 * 60 * 2)  # 2시간마다 실행
    except Exception as e:
        print("❌ Error:", e)
        time.sleep(60)
