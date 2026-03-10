#!/usr/bin/env python3
"""
볼린저밴드 하단 터치 롱 전략
- BB(20,2) 하단 터치 시 롱 진입
- RSI 과매도 확인 (< 35)
- ATR 기반 TP/SL
"""

import os
import time
import random
import ccxt
import pandas as pd
import numpy as np
from datetime import datetime

# ── API 설정 ──────────────────────────────────────────────────────────────────
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

exchange = ccxt.bybit({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "timeout": 10000,
    "options": {"defaultType": "linear"},
})

# ── 설정 ─────────────────────────────────────────────────────────────────────
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
TIMEFRAME = "15m"
LEVERAGE = 3
BB_PERIOD = 20
BB_STD = 2.0
RSI_PERIOD = 14
RSI_THRESHOLD = 35
ATR_PERIOD = 14
TP_ATR_MULT = 2.0      # TP = 진입가 + ATR * 2
SL_ATR_MULT = 1.0      # SL = 진입가 - ATR * 1
LOOP_INTERVAL = 60      # 초

# ── 포지션 관리 ──────────────────────────────────────────────────────────────
open_positions = {}     # {symbol: {side, entry_price, tp, sl, time}}


# ── 지표 계산 ────────────────────────────────────────────────────────────────
def calc_bb(close, period=BB_PERIOD, std=BB_STD):
    sma = close.rolling(period).mean()
    std_dev = close.rolling(period).std()
    upper = sma + std * std_dev
    lower = sma - std * std_dev
    return upper, sma, lower


def calc_rsi(close, period=RSI_PERIOD):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def calc_atr(high, low, close, period=ATR_PERIOD):
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ── API 호출 (재시도) ────────────────────────────────────────────────────────
def safe_fetch(func, *args, retries=3):
    for i in range(retries):
        try:
            return func(*args)
        except Exception as e:
            print(f"  API error {func.__name__} ({i+1}/{retries}): {e}")
            time.sleep(random.randint(2, 5))
    return None


# ── 데이터 조회 & 지표 ──────────────────────────────────────────────────────
def get_data(symbol):
    data = safe_fetch(exchange.fetch_ohlcv, symbol, TIMEFRAME, 100)
    if not data:
        return None

    df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "volume"])
    df["bb_upper"], df["bb_mid"], df["bb_lower"] = calc_bb(df["close"])
    df["rsi"] = calc_rsi(df["close"])
    df["atr"] = calc_atr(df["high"], df["low"], df["close"])
    return df.dropna()


# ── 신호 감지 ────────────────────────────────────────────────────────────────
def check_signal(df):
    """
    롱 진입 조건:
    1. 이전 캔들 종가 > BB 하단 (밴드 밖이 아니었음)
    2. 현재 캔들 종가 <= BB 하단 (하단 터치)
    3. RSI < 35 (과매도)
    """
    curr = df.iloc[-1]
    prev = df.iloc[-2]

    touch_lower = prev["close"] > prev["bb_lower"] and curr["close"] <= curr["bb_lower"]
    rsi_oversold = curr["rsi"] < RSI_THRESHOLD

    if touch_lower and rsi_oversold:
        return "LONG"
    return None


# ── TP/SL 체크 ───────────────────────────────────────────────────────────────
def check_exit(symbol, current_price):
    if symbol not in open_positions:
        return None

    pos = open_positions[symbol]
    if current_price >= pos["tp"]:
        return "TP"
    if current_price <= pos["sl"]:
        return "SL"
    return None


# ── 주문 실행 ────────────────────────────────────────────────────────────────
def execute_entry(symbol, price, atr):
    tp = price + atr * TP_ATR_MULT
    sl = price - atr * SL_ATR_MULT

    open_positions[symbol] = {
        "side": "LONG",
        "entry_price": price,
        "tp": tp,
        "sl": sl,
        "time": datetime.utcnow().strftime("%H:%M:%S"),
    }

    print(f"  >> LONG {symbol} @ {price:.2f} | TP {tp:.2f} | SL {sl:.2f}")

    # ── 실거래 전환 시 아래 주석 해제 ──
    # size = (equity_per_slot * LEVERAGE) / price
    # exchange.create_market_order(symbol, "buy", size, params={
    #     "takeProfitPrice": str(round(tp, 2)),
    #     "stopLossPrice": str(round(sl, 2)),
    # })


def execute_exit(symbol, price, reason):
    pos = open_positions.pop(symbol)
    pnl = (price / pos["entry_price"] - 1) * 100 * LEVERAGE
    print(f"  << EXIT {symbol} [{reason}] @ {price:.2f} | PnL {pnl:+.2f}%")

    # ── 실거래 전환 시 아래 주석 해제 ──
    # exchange.create_market_order(symbol, "sell", size)


# ── 메인 루프 ────────────────────────────────────────────────────────────────
def main():
    print(f"BB Lower Touch Long Strategy | {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"Symbols: {SYMBOLS} | TF: {TIMEFRAME} | Leverage: {LEVERAGE}x")
    print(f"BB({BB_PERIOD},{BB_STD}) | RSI<{RSI_THRESHOLD} | TP={TP_ATR_MULT}ATR SL={SL_ATR_MULT}ATR")
    print()

    loop = 0
    while True:
        loop += 1
        now = datetime.utcnow().strftime("%H:%M:%S")
        print(f"[{now}] Loop #{loop} | Positions: {list(open_positions.keys())}")

        for symbol in SYMBOLS:
            df = get_data(symbol)
            if df is None or df.empty:
                continue

            price = df["close"].iloc[-1]
            atr = df["atr"].iloc[-1]
            rsi = df["rsi"].iloc[-1]
            bb_lower = df["bb_lower"].iloc[-1]

            # 청산 체크
            exit_reason = check_exit(symbol, price)
            if exit_reason:
                execute_exit(symbol, price, exit_reason)

            # 진입 체크 (포지션 없을 때만)
            if symbol not in open_positions:
                signal = check_signal(df)
                if signal:
                    execute_entry(symbol, price, atr)
                else:
                    print(f"  {symbol} | {price:.2f} | RSI {rsi:.1f} | BB- {bb_lower:.2f} | -")

        time.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    main()
