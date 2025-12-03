# ==========================================
# Quant Momentum v3.2R - 1min Stable Debug Edition
# ==========================================

import os, time, random, pandas as pd, ccxt
from datetime import datetime

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

exchange = ccxt.bybit({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "rateLimit": 2000,            # 최소 2초 간격 요청
    "timeout": 10000,             # 10초 후 타임아웃
    "urls": {"api": "https://api.bybitglobal.com"},
    "options": {"defaultType": "linear"}
})

TIMEFRAME = "1m"
BASE_TP, BASE_SL = 0.025, 0.015
MAX_SLOTS = 4
SYMBOLS = ["BTC/USDT","ETH/USDT","SOL/USDT"]
LOG_FILE = "tradelog_v3.2R_live.csv"

def safe_fetch(func, *args, retries=3, wait=(2,5)):
    for i in range(retries):
        try:
            print(f"🕒 API call {i+1}/{retries} → {func.__name__}")
            return func(*args)
        except Exception as e:
            print(f"⚠️ API Error ({i+1}/{retries}): {e}")
            time.sleep(random.randint(*wait))
    print(f"❌ {func.__name__} failed after {retries} retries")
    return None

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

def get_ohlcv(symbol):
    print(f"📊 Fetching OHLCV for {symbol} ({TIMEFRAME})")
    data = safe_fetch(exchange.fetch_ohlcv, symbol, TIMEFRAME, 200)
    if not data: return None
    df = pd.DataFrame(data, columns=["ts","open","high","low","close","volume"])
    df["rsi"] = ta_rsi(df["close"])
    df["macd"], df["macd_signal"] = ta_macd(df["close"])
    df["atr20"] = ta_atr(df["high"], df["low"], df["close"], 20)
    df["candle_score"] = ((df["close"] - df["open"]) /
                          (df["high"] - df["low"] + 1e-9)) * 10
    return df.dropna()

def get_signal(df):
    last, prev = df.iloc[-1], df.iloc[-2]
    long_cond  = (last["rsi"] < 40) and (prev["macd"] < prev["macd_signal"]) \
                 and (last["macd"] > last["macd_signal"]) and (last["candle_score"] >= 5)
    short_cond = (last["rsi"] > 60) and (prev["macd"] > prev["macd_signal"]) \
                 and (last["macd"] < last["macd_signal"]) and (last["candle_score"] <= -5)
    if long_cond: return "long"
    if short_cond: return "short"
    return None

def execute_trade(symbol, signal):
    try:
        balance = safe_fetch(exchange.fetch_balance)
        usdt = balance["total"]["USDT"]
        price = safe_fetch(exchange.fetch_ticker, symbol)["last"]
        size = usdt / 4 / price
        side = "buy" if signal == "long" else "sell"
        tp = price * (1 + BASE_TP) if signal == "long" else price * (1 - BASE_TP)
        sl = price * (1 - BASE_SL) if signal == "long" else price * (1 + BASE_SL)

        print(f"📈 {signal.upper()} {symbol} | Entry {price:.2f} | TP {tp:.2f} | SL {sl:.2f}")
        safe_fetch(exchange.create_market_order, symbol, side, size)
        log_trade(signal, symbol, price, tp, sl)
    except Exception as e:
        print(f"💥 Trade error on {symbol}: {e}")

def log_trade(side, symbol, price, tp, sl):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    entry = pd.DataFrame([{"time": now, "side": side, "symbol": symbol,
                           "entry": price, "tp": tp, "sl": sl}])
    entry.to_csv(LOG_FILE, mode="a", header=not os.path.exists(LOG_FILE), index=False)
    print(f"🧾 Logged {side} {symbol} @ {price:.2f}")

# ---- 메인 루프 (1분봉용, 60초 주기) ----
loop = 0
while True:
    loop += 1
    print(f"\n[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] 🔄 Loop #{loop}")
    try:
        for s in SYMBOLS:
            df = get_ohlcv(s)
            if df is None:
                print(f"⚠️ No data for {s}")
                continue
            signal = get_signal(df)
            if signal:
                execute_trade(s, signal)
            else:
                rsi, macd = df["rsi"].iloc[-1], df["macd"].iloc[-1]
                print(f"⚪ No signal: {s} (RSI={rsi:.1f}, MACD={macd:.4f})")
        print("✅ Cycle complete. Sleeping 60s...\n")
        time.sleep(60)
    except Exception as e:
        print(f"⚠️ Main loop error: {e}")
        time.sleep(15)
