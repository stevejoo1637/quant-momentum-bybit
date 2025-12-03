# =============================
#  Bybit Quant Momentum Bot
#  Deploy-Ready (Render Version)
# =============================

import os
import time
import ccxt

# -----------------------------
# 1. 환경변수에서 API 키 불러오기
# -----------------------------
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

print("🚀 Starting Quant Momentum Bot...")

if not API_KEY or not API_SECRET:
    raise ValueError("❌ API 키 또는 시크릿이 비어 있습니다. Render Environment Variables를 확인하세요.")

# -----------------------------
# 2. Bybit 객체 생성
# -----------------------------
exchange = ccxt.bybit({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "linear"}  # 선물(USDT) 전용
})

# -----------------------------
# 3. 거래 설정
# -----------------------------
symbol = "BTC/USDT"   # 거래 페어
leverage = 10         # 레버리지 배율
balance_ratio = 0.1   # 계좌의 10%만 사용

# -----------------------------
# 4. 레버리지 설정
# -----------------------------
try:
    markets = exchange.load_markets()
    market = exchange.market(symbol)
    if market['type'] != 'linear':
        raise Exception("⚠️ 지원되지 않는 시장 유형입니다. (linear only)")
    
    exchange.set_leverage(leverage, symbol)
    print(f"✅ {symbol} 레버리지 {leverage}x 설정 완료")
except Exception as e:
    print(f"❌ 레버리지 설정 실패: {e}")

# -----------------------------
# 5. 메인 루프 (전략 실행)
# -----------------------------
def get_balance():
    try:
        balance = exchange.fetch_balance()
        usdt = balance['total']['USDT']
        print(f"💰 잔고: {usdt:.2f} USDT")
        return usdt
    except Exception as e:
        print(f"❌ 잔고 조회 실패: {e}")
        return 0

def get_signal():
    # 단순 모멘텀 예시 전략
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=50)
    closes = [c[4] for c in ohlcv]
    sma_fast = sum(closes[-5:]) / 5
    sma_slow = sum(closes[-20:]) / 20
    if sma_fast > sma_slow:
        return "buy"
    elif sma_fast < sma_slow:
        return "sell"
    else:
        return "hold"

def trade(signal):
    try:
        balance = get_balance()
        amount = (balance * balance_ratio) / exchange.fetch_ticker(symbol)['last']

        if signal == "buy":
            print(f"📈 매수 시그널 발생 — {amount:.4f} {symbol}")
            order = exchange.create_market_buy_order(symbol, amount)
            print("✅ 매수 주문 완료:", order)
        elif signal == "sell":
            print(f"📉 매도 시그널 발생 — {amount:.4f} {symbol}")
            order = exchange.create_market_sell_order(symbol, amount)
            print("✅ 매도 주문 완료:", order)
        else:
            print("⏸️ 대기 중... (시그널 없음)")
    except Exception as e:
        print(f"⚠️ 주문 실패: {e}")

# -----------------------------
# 6. 루프 실행
# -----------------------------
while True:
    try:
        signal = get_signal()
        trade(signal)
        time.sleep(60 * 5)  # 5분마다 반복
    except Exception as e:
        print(f"🚨 루프 오류 발생: {e}")
        time.sleep(30)
