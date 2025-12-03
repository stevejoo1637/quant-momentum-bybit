# ==========================================
# Quant Momentum v3.4R - 실전 자동매매
# ------------------------------------------
# ✅ 실제 주문 실행 + TP/SL 자동 설정
# ✅ 포지션 관리 + 중복 방지
# ✅ 안전 장치 다수 적용
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
    "timeout": 15000,
    "rateLimit": 2000,
    "options": {"defaultType": "linear"},
    "urls": {
        "api": {
            "public": "https://api.bybit.com",
            "private": "https://api.bybit.com"
        }
    }
})

print("💰 MAINNET 실거래 모드 활성화!")
print(f"🔗 API URL: https://api.bybit.com")

# ---- 기본 설정 ----
TIMEFRAME = "1m"
LEVERAGE = 3
BASE_TP_PCT = 2.5  # 익절 2.5%
BASE_SL_PCT = 1.5  # 손절 1.5%
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
MAX_POSITIONS = 3  # 최대 동시 포지션 수
ORDER_SIZE_USDT = 20  # 각 주문당 USDT 금액

# 전역 포지션 추적
active_positions = {}

# ==========================================
# 지표 계산 함수
# ==========================================
def ta_rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(n).mean()
    avg_loss = loss.rolling(n).mean()
    rs = avg_gain / (avg_loss + 1e-9)
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
# 안전한 API 호출
# ==========================================
def safe_api_call(func, *args, retries=3, **kwargs):
    for i in range(retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            print(f"⚠️ API 오류 ({i+1}/{retries}): {e}")
            time.sleep(random.randint(2, 5))
    return None

# ==========================================
# 초기화: API 연결 확인
# ==========================================
def check_api_connection():
    try:
        balance = exchange.fetch_balance()
        usdt_balance = balance['USDT']['free']
        print(f"✅ API 연결 성공 | 잔고: {usdt_balance:.2f} USDT")
        return True
    except Exception as e:
        print(f"❌ API 연결 실패: {e}")
        return False

# ==========================================
# 레버리지 설정
# ==========================================
def set_leverage(symbol):
    try:
        exchange.set_leverage(LEVERAGE, symbol)
        print(f"✅ {symbol} 레버리지 {LEVERAGE}x 설정")
    except Exception as e:
        print(f"⚠️ 레버리지 설정 실패 ({symbol}): {e}")

# ==========================================
# OHLCV 데이터 불러오기
# ==========================================
def get_ohlcv(symbol):
    data = safe_api_call(exchange.fetch_ohlcv, symbol, TIMEFRAME, 200)
    if not data:
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
    if len(df) < 2:
        return None
    
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
# 현재 포지션 확인
# ==========================================
def get_current_positions():
    try:
        positions = exchange.fetch_positions()
        active = {}
        for pos in positions:
            if float(pos['contracts']) > 0:
                active[pos['symbol']] = {
                    'side': pos['side'],
                    'size': float(pos['contracts']),
                    'entry': float(pos['entryPrice'])
                }
        return active
    except Exception as e:
        print(f"⚠️ 포지션 조회 실패: {e}")
        return {}

# ==========================================
# 주문 실행 (TP/SL 포함)
# ==========================================
def execute_trade(symbol, signal, price):
    global active_positions
    
    # 중복 진입 방지
    if symbol in active_positions:
        print(f"⚠️ {symbol} 이미 포지션 보유중 - 스킵")
        return
    
    # 최대 포지션 수 제한
    if len(active_positions) >= MAX_POSITIONS:
        print(f"⚠️ 최대 포지션 수({MAX_POSITIONS}) 도달 - 스킵")
        return
    
    try:
        # 주문 수량 계산
        amount = (ORDER_SIZE_USDT * LEVERAGE) / price
        amount = round(amount, 3)  # 소수점 3자리
        
        # TP/SL 가격 계산
        if signal == "LONG":
            tp_price = price * (1 + BASE_TP_PCT / 100)
            sl_price = price * (1 - BASE_SL_PCT / 100)
            side = "buy"
        else:
            tp_price = price * (1 - BASE_TP_PCT / 100)
            sl_price = price * (1 + BASE_SL_PCT / 100)
            side = "sell"
        
        # 레버리지 설정
        set_leverage(symbol)
        
        # 시장가 주문 실행
        order = exchange.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=amount,
            params={
                "takeProfit": round(tp_price, 2),
                "stopLoss": round(sl_price, 2)
            }
        )
        
        active_positions[symbol] = {
            'side': signal,
            'entry': price,
            'tp': tp_price,
            'sl': sl_price,
            'time': datetime.utcnow()
        }
        
        print(f"🚀 [{signal}] {symbol} 진입 완료")
        print(f"   진입가: {price:.2f} | TP: {tp_price:.2f} | SL: {sl_price:.2f}")
        
    except Exception as e:
        print(f"❌ 주문 실행 실패 ({symbol}): {e}")

# ==========================================
# 포지션 정리 (TP/SL 체크)
# ==========================================
def check_and_close_positions():
    global active_positions
    current_positions = get_current_positions()
    
    # 청산된 포지션 제거
    closed = [sym for sym in active_positions if sym not in current_positions]
    for sym in closed:
        pos = active_positions[sym]
        print(f"✅ {sym} 포지션 청산됨 ({pos['side']})")
        del active_positions[sym]

# ==========================================
# 메인 루프
# ==========================================
def main():
    global active_positions
    
    print(f"🚀 Quant Momentum v3.4R 시작 ({datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')})")
    
    # API 연결 확인
    if not check_api_connection():
        print("❌ API 연결 실패 - 프로그램 종료")
        return
    
    loop = 0
    while True:
        loop += 1
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n{'='*60}")
        print(f"💓 Loop #{loop} | UTC {now}")
        print(f"📊 활성 포지션: {len(active_positions)}/{MAX_POSITIONS}")
        
        try:
            # 포지션 상태 업데이트
            check_and_close_positions()
            
            # 각 심볼 분석
            for sym in SYMBOLS:
                df = get_ohlcv(sym)
                if df is None:
                    continue
                
                signal = get_signal(df)
                price = df["close"].iloc[-1]
                rsi = df["rsi"].iloc[-1]
                
                if signal:
                    print(f"🎯 {sym} | 신호: {signal} | RSI: {rsi:.1f} | 가격: {price:.2f}")
                    execute_trade(sym, signal, price)
                else:
                    print(f"⚪ {sym} | RSI: {rsi:.1f} | 신호 없음")
            
            print(f"\n✅ Loop 완료 | 60초 대기...")
            time.sleep(60)
            
        except KeyboardInterrupt:
            print("\n⚠️ 사용자 중단 - 프로그램 종료")
            break
        except Exception as e:
            print(f"💥 메인 루프 오류: {e}")
            time.sleep(15)

if __name__ == "__main__":
    main()
