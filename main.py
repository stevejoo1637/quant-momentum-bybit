import ccxt
import pandas as pd
import numpy as np
import time
import requests
import ta
import os
import hmac
import hashlib
import time as t

# ===== 환경 변수 =====
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_SECRET_KEY")

# ===== Bybit 선물용 설정 =====
symbol = "BTCUSDT"  # Unified Account에서는 :USDT 필요 없음
timeframe = "15m"
amount = 0.001
leverage = 5

exchange = ccxt.bybit({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "options": {
        "defaultType": "linear"
    }
})

# ===== 레버리지 설정 (REST API 직접 호출) =====
def set_leverage(symbol, leverage):
    url = "https://api.bybit.com/v5/position/set-leverage"
    ts = int(t.time() * 1000)
    body = {
        "category": "linear",
        "symbol": symbol,
        "buyLeverage": str(leverage),
        "sellLeverage": str(leverage)
    }

    param_str = f"api_key={API_KEY}&buyLeverage={leverage}&category=linear&recv_window=5000&sellLeverage={leverage}&symbol={symbol}&timestamp={ts}"
    signature = hmac.new(
        bytes(API_SECRET, "utf-8"),
        bytes(param_str, "utf-8"),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-SIGN": signature,
        "X-BAPI-TIMESTAMP": str(ts),
        "X-BAPI-RECV-WINDOW": "5000"
    }

    try:
        res = requests.post(url, json=body, headers=headers)
        print("✅ Leverage Set Response:", res.json())
    except Exception as e:
        print("⚠️ Leverage setup error:", e)

set_leverage(symbol, leverage)

# ===== 전략 (단순 이동평균 교차) =====
def get_signal():
    bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
    df = pd.DataFrame(bars, columns=["time", "open", "high", "low", "close", "volume"])
    df["MA5"] = df["close"].rolling(5).mean()
    df["MA20"] = df["close"].rolling(20).mean()
    if df["MA5"].iloc[-1] > df["MA20"].iloc[-1]:
        return "buy"
    elif df["MA5"].iloc[-1] < df["MA20"].iloc[-1]:
        return "sell"
    else:
        return None

def close_all_positions():
    try:
        positions = exchange.fetch_positions([symbol])
        for p in positions:
            if float(p["contracts"]) > 0:
                side = "sell" if p["side"] == "long" else "buy"
                exchange.create_market_order(symbol, side, abs(float(p["contracts"])))
                print(f"🚪 Closed {p['side']} position")
    except Exception as e:
        print("⚠️ Close positions error:", e)

print("🤖 Bybit Unified Account Futures Bot Started!")

while True:
    try:
        signal = get_signal()
        if signal == "buy":
            close_all_positions()
            order = exchange.create_market_order(symbol, "buy", amount)
            print(f"✅ Long opened! {order['id']}")
        elif signal == "sell":
            close_all_positions()
            order = exchange.create_market_order(symbol, "sell", amount)
            print(f"✅ Short opened! {order['id']}")
        else:
            print("⏳ Waiting...")

    except Exception as e:
        print("⚠️ Error:", e)
    time.sleep(60)
