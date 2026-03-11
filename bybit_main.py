#!/usr/bin/env python3
"""
바이비트 선물 채널 돌파 복합 전략 자동매매

전략:
  A: 상단돌파 롱 (강세, SL-7%/TP+25%/7일, R²>0.5, 볼륨1.5x)
  B: 하단돌파 롱 (무관, SL-5%/TP+15%/14일, R²>0.5, 볼륨1.0x)
  C: 상단터치 숏 (약세, SL-15%/TP+20%/10일, R²>0.3, 볼륨1.0x)

설정:
  레버리지: 2x | 슬롯: 4 | 현금비율: 40%
  시장필터: BTC SMA20 > SMA50
  유니버스: 거래대금 상위 60 (BTC/ETH 제외, 상장 150일 미만 제외)

일정 (24/7):
  00:05 UTC → 일간 체크 (시그널 생성 + 진입/청산)
  매 5분    → SL/TP 모니터링
"""

import os
import sys
import json
import time
import math
import logging
import schedule
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta

from bybit_api import BybitAPI

# ── 설정 ─────────────────────────────────────────────────────────────────────

API_KEY    = os.environ.get("BYBIT_API_KEY", "")
API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
TESTNET    = os.environ.get("BYBIT_TESTNET", "0") == "1"

LEVERAGE   = 2
MAX_POS    = 4
CASH_RATIO = 0.40
MDD_DEPLOY_THRESH = -0.35  # MDD -35% 도달 시 현금 전량투입
TOP_N      = 60
MIN_LIST_DAYS = 150  # 상장 150일 미만 종목 제외
EXCLUDE    = {"BTCUSDT", "ETHUSDT"}

STRATS = {
    "A": {
        "name": "상단돌파 롱",
        "signal": "upper_break",
        "direction": "long",
        "btc_filter": "bull",
        "sl": 0.07, "tp": 0.25, "hold_days": 7,
        "r2_thresh": 0.5, "vol_mult": 1.5,
    },
    "B": {
        "name": "하단돌파 롱",
        "signal": "lower_break",
        "direction": "long",
        "btc_filter": "none",
        "sl": 0.05, "tp": 0.15, "hold_days": 14,
        "r2_thresh": 0.5, "vol_mult": 1.0,
    },
    "C": {
        "name": "상단터치 숏",
        "signal": "upper_touch",
        "direction": "short",
        "btc_filter": "bear",
        "sl": 0.15, "tp": 0.20, "hold_days": 10,
        "r2_thresh": 0.3, "vol_mult": 1.0,
    },
}

CHANNEL_PERIOD = 20
CHANNEL_STD    = 2.0

BASE_DIR  = os.environ.get("BYBIT_BASE_DIR", "/root/bybit_strategy")
STATE_F   = f"{BASE_DIR}/state.json"
LOG_F     = f"{BASE_DIR}/trading.log"

DRY_RUN   = os.environ.get("BYBIT_DRY_RUN", "0") == "1"

# ── 텔레그램 ─────────────────────────────────────────────────────────────────

TG_TOKEN   = os.environ.get("TG_TOKEN", "8572380635:AAEz-A1b84rmLKr9r40onzyCqEEbKJDTGns")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "5579958607")
TG_GROUP_ID = os.environ.get("TG_GROUP_ID", "-5144226997")


def tg_send(msg: str):
    """텔레그램 메시지 전송 (개인 + 그룹)"""
    for chat_id in [TG_GROUP_ID]:
        if not chat_id:
            continue
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception as e:
            log.warning(f"텔레그램 전송 실패 (chat_id={chat_id}): {e}")

# ── 로깅 ─────────────────────────────────────────────────────────────────────

os.makedirs(BASE_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_F, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── API 클라이언트 ────────────────────────────────────────────────────────────

api = BybitAPI(API_KEY, API_SECRET, testnet=TESTNET)

# ── 상태 관리 ─────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_F):
        with open(STATE_F, encoding="utf-8") as f:
            return json.load(f)
    return {
        "positions": {},
        # {symbol: {strat, direction, entry_price, qty, entry_date,
        #           sl_price, tp_price, side}}
        "universe": [],
        "last_universe_date": "",
    }


def save_state(s: dict):
    with open(STATE_F, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


# ── 유틸 ─────────────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today_str() -> str:
    return now_utc().strftime("%Y-%m-%d")


def days_since(date_str: str) -> int:
    entry = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (now_utc() - entry).days


def round_qty(qty: float, step: float) -> str:
    """수량을 step 단위로 내림"""
    if step <= 0:
        return str(qty)
    precision = max(0, -int(math.floor(math.log10(step))))
    rounded = math.floor(qty / step) * step
    return f"{rounded:.{precision}f}"


# ── 채널 계산 ─────────────────────────────────────────────────────────────────

def calc_channel(closes: list[float]) -> dict | None:
    """최근 CHANNEL_PERIOD개 종가로 선형회귀 채널 계산"""
    n = len(closes)
    if n < CHANNEL_PERIOD:
        return None

    y = np.array(closes[-CHANNEL_PERIOD:])
    if np.isnan(y).any():
        return None

    x = np.arange(CHANNEL_PERIOD)
    x_mean = x.mean()
    x_var = ((x - x_mean) ** 2).sum()
    y_mean = y.mean()

    slope = ((x - x_mean) * (y - y_mean)).sum() / x_var
    intercept = y_mean - slope * x_mean
    trend_vals = slope * x + intercept
    resid = y - trend_vals
    std_r = resid.std()

    ss_res = (resid ** 2).sum()
    ss_tot = ((y - y_mean) ** 2).sum()
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

    return {
        "upper": trend_vals[-1] + CHANNEL_STD * std_r,
        "lower": trend_vals[-1] - CHANNEL_STD * std_r,
        "r2": r2,
    }


# ── BTC 시장 필터 ─────────────────────────────────────────────────────────────

def get_btc_market_state():
    """BTC SMA20 > SMA50 → True(강세) / False(약세) / None(조회실패)"""
    for attempt in range(3):
        try:
            klines = api.get_klines("BTCUSDT", interval="D", limit=55)
            closes = [float(k[4]) for k in klines]
            if len(closes) < 50:
                log.warning(f"BTC 일봉 부족: {len(closes)}개 (시도 {attempt+1}/3)")
                if attempt < 2:
                    time.sleep(5)
                continue
            sma20 = np.mean(closes[-20:])
            sma50 = np.mean(closes[-50:])
            is_bull = sma20 > sma50
            log.info(f"BTC 시장: SMA20={sma20:.0f} {'>' if is_bull else '<='} SMA50={sma50:.0f} → {'강세' if is_bull else '약세'}")
            return is_bull
        except Exception as e:
            log.warning(f"BTC 시장 상태 조회 실패 (시도 {attempt+1}/3): {e}")
            if attempt < 2:
                time.sleep(5)
    log.error("BTC 시장 상태 3회 조회 실패 → 진입 스킵")
    tg_send("⚠️ BTC 시장필터 3회 연속 실패\n연결 오류 또는 데이터 부족으로 진입을 스킵합니다.")
    return None


# ── 유니버스 ──────────────────────────────────────────────────────────────────

def update_universe(state: dict) -> list[str]:
    """전년 평균 거래대금 상위 TOP_N 종목 선정 (연 1회 갱신) — 백테스트 동일"""
    today = today_str()
    current_year = today[:4]
    if (state.get("universe") and state.get("last_universe_year")
            and current_year == state.get("last_universe_year")):
        return state["universe"]

    log.info("유니버스 갱신 중 (전년 평균 거래대금 기준)...")
    try:
        # 상장일 조회 → 상장 MIN_LIST_DAYS일 미만 제외
        instruments = api.session.get_instruments_info(category="linear")
        now_ms = int(time.time() * 1000)
        min_launch_ms = now_ms - MIN_LIST_DAYS * 86400 * 1000

        candidates = []
        for item in instruments["result"]["list"]:
            sym = item["symbol"]
            if not sym.endswith("USDT") or sym in EXCLUDE:
                continue
            if item.get("status") != "Trading":
                continue
            lt = int(item.get("launchTime", "0") or "0")
            if lt > min_launch_ms:
                continue
            candidates.append(sym)

        log.info(f"  후보 종목: {len(candidates)}개 (D{MIN_LIST_DAYS} 필터 후)")

        # 각 종목의 전년도 일봉 거래대금(close*volume) 평균 계산
        prev_year = str(int(current_year) - 1)
        start_ms = int(pd.Timestamp(f"{prev_year}-01-01").timestamp() * 1000)
        end_ms = int(pd.Timestamp(f"{prev_year}-12-31").timestamp() * 1000)

        avg_turnover = {}
        for i, sym in enumerate(candidates):
            try:
                # 전년도 일봉 전체 다운로드 (start ~ end)
                all_klines = []
                fetch_end = end_ms
                for _ in range(3):  # 최대 3번 페이징
                    resp = api.session.get_kline(
                        category="linear", symbol=sym,
                        interval="D", limit=200,
                        start=start_ms, end=fetch_end,
                    )
                    rows = resp["result"]["list"]
                    if not rows:
                        break
                    all_klines.extend(rows)
                    earliest = int(rows[-1][0])
                    if earliest <= start_ms:
                        break
                    fetch_end = earliest - 1
                    time.sleep(0.03)

                tv_sum = 0.0
                tv_count = 0
                for k in all_klines:
                    ts = int(k[0])
                    if start_ms <= ts <= end_ms:
                        close_p = float(k[4])
                        vol = float(k[5])
                        tv_sum += close_p * vol
                        tv_count += 1
                if tv_count >= 100:
                    avg_turnover[sym] = tv_sum / tv_count
            except Exception:
                pass
            if (i + 1) % 50 == 0:
                log.info(f"  ... {i+1}/{len(candidates)}")
            time.sleep(0.05)

        # 평균 거래대금 상위 TOP_N
        ranked = sorted(avg_turnover.items(), key=lambda x: -x[1])
        universe = [sym for sym, _ in ranked[:TOP_N]]

        state["universe"] = universe
        state["last_universe_year"] = current_year
        save_state(state)
        log.info(f"유니버스: {len(universe)}종목 (상위: {universe[:5]})")
        return universe
    except Exception as e:
        log.error(f"유니버스 갱신 실패: {e}")
        return state.get("universe", [])


# ── 포지션 청산 ───────────────────────────────────────────────────────────────

def close_pos(symbol: str, state: dict, reason: str):
    """포지션 청산"""
    pos = state["positions"].get(symbol)
    if not pos:
        return
    side = pos["side"]  # "Buy" or "Sell"
    qty = str(pos["qty"])

    entry = pos["entry_price"]
    direction = pos["direction"]
    try:
        ticker_data = api.get_ticker(symbol)
        cur_price = float(ticker_data["lastPrice"])
    except Exception:
        cur_price = entry

    if direction == "short":
        pnl = -(cur_price / entry - 1) * 100
    else:
        pnl = (cur_price / entry - 1) * 100

    if DRY_RUN:
        log.info(f"[DRY] 청산: {symbol} {side} qty={qty} [{reason}]")
    else:
        try:
            api.close_position(symbol, side, qty)
            log.info(f"청산: {symbol} {side} qty={qty} [{reason}]")
        except Exception as e:
            log.error(f"청산 실패: {symbol} {e}")
            return

    # 청산 알림
    sk = pos['strat']
    cfg = STRATS[sk]
    dir_tag = "숏" if direction == "short" else "롱"
    pnl_emoji = "✅" if pnl >= 0 else "❌"

    # 사유 한글화 — BTC필터는 전환 방향 명시
    reason_kr = reason
    if reason.startswith("BTC필터"):
        btcf = reason.split("(")[-1].rstrip(")")  # "bull" or "bear"
        if btcf == "bear":
            reason_kr = "BTC 강세전환"
        else:
            reason_kr = "BTC 약세전환"
    elif reason.startswith("SL"):
        reason_kr = f"손절 {reason.split(' ')[-1] if ' ' in reason else ''}"
    elif reason.startswith("TP"):
        reason_kr = f"익절 {reason.split(' ')[-1] if ' ' in reason else ''}"
    elif reason.startswith("TIME"):
        reason_kr = f"기간만료"

    held = days_since(pos.get("entry_date", today_str()))

    # 가상 누적수익률 기록 (1슬롯당 1/MAX_POS 비중)
    weight = 1.0 / MAX_POS
    weighted_pnl = pnl * weight
    trade_log = state.setdefault("trade_log", [])
    trade_log.append({
        "symbol": symbol, "strat": sk, "dir": direction,
        "entry": entry, "exit": cur_price, "pnl": pnl,
        "wpnl": weighted_pnl, "reason": reason, "held": held,
        "date": today_str(),
    })

    tg_send(
        f"{pnl_emoji} <b>청산</b> {symbol}\n"
        f"{sk}{dir_tag} | {reason_kr}\n"
        f"${entry:.4f} → ${cur_price:.4f}\n"
        f"수익: <b>{pnl:+.1f}%</b> ({held}일)"
    )
    del state["positions"][symbol]


# ── 일간 체크 (00:05 UTC) ────────────────────────────────────────────────────

def daily_check():
    log.info("=" * 60)
    log.info("  일간 체크 시작")
    log.info("=" * 60)

    state = load_state()

    # 1. BTC 시장 상태
    is_bull = get_btc_market_state()

    # 2. 유니버스 갱신
    universe = update_universe(state)
    if not universe:
        log.warning("유니버스 비어있음 → 스킵")
        return

    # 3. 종목 정보 (최소수량 등)
    try:
        instruments = api.get_instruments()
    except Exception as e:
        log.error(f"종목 정보 조회 실패: {e}")
        return

    # 4. BTC 필터 청산 — 시장 상태 변경 시 (조회 실패 시 청산/진입 안 함)
    if is_bull is None:
        log.warning("BTC 시장필터 조회 실패 → 청산/진입 스킵")
        save_state(state)
        return
    for sym in list(state["positions"].keys()):
        pos = state["positions"][sym]
        sk = pos["strat"]
        btcf = STRATS[sk]["btc_filter"]
        if (btcf == "bull" and not is_bull) or (btcf == "bear" and is_bull):
            close_pos(sym, state, f"BTC필터({btcf})")

    # 5. SL/TP/TIME 청산
    for sym in list(state["positions"].keys()):
        pos = state["positions"][sym]
        try:
            ticker = api.get_ticker(sym)
            cur_price = float(ticker["lastPrice"])
        except Exception as e:
            log.error(f"{sym} 현재가 조회 실패: {e}")
            continue

        entry = pos["entry_price"]
        direction = pos["direction"]
        if direction == "short":
            pnl = -(cur_price / entry - 1)
        else:
            pnl = cur_price / entry - 1

        held = days_since(pos["entry_date"])
        sk = pos["strat"]
        cfg = STRATS[sk]

        if pnl <= -cfg["sl"]:
            close_pos(sym, state, f"SL {pnl*100:+.1f}%")
        elif pnl >= cfg["tp"]:
            close_pos(sym, state, f"TP {pnl*100:+.1f}%")
        elif held >= cfg["hold_days"]:
            close_pos(sym, state, f"TIME {held}일")

    save_state(state)

    # 6. 신규 진입
    avail_slots = MAX_POS - len(state["positions"])
    if avail_slots <= 0:
        log.info("빈 슬롯 없음 → 진입 스킵")
        return

    # 자산 조회
    try:
        equity = api.get_equity()
        log.info(f"총 자산: ${equity:,.2f}")
    except Exception as e:
        log.error(f"자산 조회 실패: {e}")
        return

    # MDD 기반 현금 비율 조정
    peak = state.get("peak_equity", equity)
    if equity > peak:
        state["peak_equity"] = equity
        peak = equity
        state["mdd_deployed"] = False  # 고점 갱신 시 트리거 리셋
    current_mdd = (equity / peak - 1) if peak > 0 else 0
    mdd_deployed = state.get("mdd_deployed", False)

    if current_mdd <= MDD_DEPLOY_THRESH and not mdd_deployed:
        effective_cash_ratio = 0.0  # 현금 전량투입
        state["mdd_deployed"] = True
        save_state(state)
        log.info(f"MDD {current_mdd*100:.1f}% → 현금 전량투입!")
        tg_send(f"⚠️ MDD {current_mdd*100:.1f}% 도달 → 현금 전량투입")
    elif mdd_deployed:
        effective_cash_ratio = 0.0  # 고점 갱신 전까지 유지
    else:
        effective_cash_ratio = CASH_RATIO

    invest_capital = equity * (1 - effective_cash_ratio)
    per_slot = invest_capital / MAX_POS

    # 후보 스캔
    candidates = []
    held_symbols = set(state["positions"].keys())

    for sym in universe:
        if sym in held_symbols:
            continue
        if sym not in instruments or instruments[sym]["status"] != "Trading":
            continue

        try:
            klines = api.get_klines(sym, interval="D", limit=25)
            if len(klines) < CHANNEL_PERIOD + 1:
                continue

            closes = [float(k[4]) for k in klines]
            volumes = [float(k[5]) for k in klines]

            channel = calc_channel(closes)
            if channel is None:
                continue

            r2 = channel["r2"]
            upper = channel["upper"]
            lower = channel["lower"]

            prev_close = closes[-2]
            curr_close = closes[-1]

            vol_ma = np.mean(volumes[-CHANNEL_PERIOD:])
            if vol_ma <= 0:
                continue
            vol_ratio = volumes[-1] / vol_ma

            # 5일 모멘텀
            if len(closes) >= 6:
                mom5 = curr_close / closes[-6] - 1
            else:
                mom5 = 0.01

            # 각 전략별 시그널 체크
            for sk, cfg in STRATS.items():
                btcf = cfg["btc_filter"]
                if (btcf == "bull" and not is_bull) or (btcf == "bear" and is_bull):
                    continue
                if r2 <= cfg["r2_thresh"] or vol_ratio <= cfg["vol_mult"]:
                    continue

                triggered = False
                sig = cfg["signal"]
                if sig == "upper_break" and prev_close <= upper and curr_close > upper:
                    triggered = True
                elif sig == "lower_break" and prev_close >= lower and curr_close < lower:
                    triggered = True
                elif sig == "upper_touch" and prev_close < upper and curr_close >= upper:
                    triggered = True

                if triggered:
                    score = r2 * vol_ratio * max(mom5, 0.01)
                    candidates.append((sym, sk, score))

        except Exception as e:
            log.debug(f"{sym} 스캔 실패: {e}")
            continue

        # API 레이트 리밋 방지
        time.sleep(0.1)

    # 점수순 정렬
    candidates.sort(key=lambda x: -x[2])
    log.info(f"시그널 후보: {len(candidates)}개")

    # 진입 실행
    entered = set(held_symbols)
    for sym, sk, score in candidates:
        if len(state["positions"]) >= MAX_POS:
            break
        if sym in entered:
            continue

        cfg = STRATS[sk]
        direction = cfg["direction"]

        try:
            ticker = api.get_ticker(sym)
            cur_price = float(ticker["lastPrice"])
            if cur_price <= 0:
                continue

            # 수량 계산
            order_usdt = per_slot * LEVERAGE
            inst = instruments.get(sym, {})
            min_qty = inst.get("min_qty", 0.001)
            qty_step = inst.get("qty_step", 0.001)
            raw_qty = order_usdt / cur_price
            qty_str = round_qty(raw_qty, qty_step)

            if float(qty_str) < min_qty:
                log.warning(f"{sym} 수량 부족: {qty_str} < {min_qty}")
                continue

            # 레버리지 설정
            api.set_leverage(sym, LEVERAGE)

            # 주문
            if DRY_RUN:
                log.info(f"[DRY] 진입: {sym} {sk}({cfg['name']}) {direction} qty={qty_str} @ ${cur_price:.4f}")
            else:
                if direction == "long":
                    api.open_long(sym, qty_str)
                else:
                    api.open_short(sym, qty_str)

            # SL/TP 가격
            if direction == "long":
                sl_price = cur_price * (1 - cfg["sl"])
                tp_price = cur_price * (1 + cfg["tp"])
                side = "Buy"
            else:
                sl_price = cur_price * (1 + cfg["sl"])
                tp_price = cur_price * (1 - cfg["tp"])
                side = "Sell"

            state["positions"][sym] = {
                "strat": sk,
                "direction": direction,
                "entry_price": cur_price,
                "qty": float(qty_str),
                "entry_date": today_str(),
                "sl_price": sl_price,
                "tp_price": tp_price,
                "side": side,
            }
            entered.add(sym)
            log.info(
                f"진입: {sym} {sk}({cfg['name']}) {direction} "
                f"qty={qty_str} @ ${cur_price:.4f} "
                f"SL=${sl_price:.4f} TP=${tp_price:.4f}"
            )
            dir_tag = "숏" if direction == "short" else "롱"
            pos_value = float(qty_str) * cur_price
            tg_send(
                f"🟢 <b>진입</b> {sym}\n"
                f"{sk}{dir_tag} | ${cur_price:.4f} × {qty_str} (${pos_value:,.1f})\n"
                f"손절: ${sl_price:.4f} (-{cfg['sl']*100:.0f}%)\n"
                f"익절: ${tp_price:.4f} (+{cfg['tp']*100:.0f}%)\n"
                f"보유한도: {cfg['hold_days']}일"
            )

        except Exception as e:
            log.error(f"{sym} 진입 실패: {e}")

    save_state(state)
    log.info(f"보유: {len(state['positions'])}포지션")
    log.info("=" * 60)

    # 텔레그램 일간 리포트
    send_daily_report(state, is_bull, equity)


# ── 일간 리포트 ───────────────────────────────────────────────────────────────

def send_daily_report(state: dict, is_bull: bool, equity: float):
    """텔레그램 일간 리포트 — 가상 누적수익률 기반"""
    positions = state.get("positions", {})
    trade_log = state.get("trade_log", [])

    # 최초 시작일 기록
    if "start_date" not in state:
        state["start_date"] = today_str()
        save_state(state)

    # ── 가상 누적수익률 (복리) ──
    # 백테스트와 동일: 각 거래의 가중수익률을 복리로 누적
    nav = 1.0  # 가상 자산 (1.0 = 100%)
    peak_nav = 1.0
    mdd = 0.0
    for t in trade_log:
        weight = 1.0 / MAX_POS
        pnl_pct = t.get("pnl", 0) / 100  # 3.0% → 0.03
        nav *= (1 + pnl_pct * weight)
        if nav > peak_nav:
            peak_nav = nav
        dd = (nav / peak_nav - 1) * 100
        if dd < mdd:
            mdd = dd

    # 미체결 포지션 평가손익 반영
    open_nav = nav
    for sym, pos in positions.items():
        try:
            ticker = api.get_ticker(sym)
            cur = float(ticker["lastPrice"])
            entry_p = pos["entry_price"]
            if pos["direction"] == "short":
                p = -(cur / entry_p - 1)
            else:
                p = cur / entry_p - 1
            open_nav *= (1 + p / MAX_POS)
        except Exception:
            pass

    total_ret = (open_nav - 1) * 100  # 누적수익률 %
    cur_dd = (open_nav / peak_nav - 1) * 100
    if cur_dd < mdd:
        mdd = cur_dd

    # CAGR
    start_date = datetime.strptime(state.get("start_date", today_str()), "%Y-%m-%d")
    days_elapsed = (now_utc().replace(tzinfo=None) - start_date).days
    if days_elapsed > 0 and open_nav > 0:
        years = days_elapsed / 365.0
        cagr = (open_nav ** (1.0 / years) - 1) * 100
    else:
        cagr = 0.0

    market = "🟢 강세" if is_bull else "🔴 약세"
    mdd_str = f"{mdd:.1f}%" if mdd < 0 else "0.0%"
    n_trades = len(trade_log)
    wins = sum(1 for t in trade_log if t.get("pnl", 0) >= 0)
    wr = wins / n_trades * 100 if n_trades > 0 else 0

    lines = [
        f"📊 <b>일간 리포트</b> {today_str()}",
        f"BTC 시장: {market}",
        f"총자산: <b>${equity:,.2f}</b>",
        f"누적수익률: <b>{total_ret:+.1f}%</b> | CAGR: {cagr:+.1f}%",
        f"MDD: {mdd_str}",
        f"거래: {n_trades}건 (승률 {wr:.0f}%)",
        f"",
        f"보유: {len(positions)}/{MAX_POS}슬롯",
    ]

    for sym, pos in positions.items():
        held = days_since(pos["entry_date"])
        cfg = STRATS[pos["strat"]]
        sk = pos["strat"]
        qty = pos.get("qty", 0)
        entry_p = pos["entry_price"]
        pos_size = qty * entry_p
        dir_tag = "숏" if pos["direction"] == "short" else "롱"
        try:
            ticker = api.get_ticker(sym)
            cur = float(ticker["lastPrice"])
            if pos["direction"] == "short":
                pnl = -(cur / entry_p - 1) * 100
            else:
                pnl = (cur / entry_p - 1) * 100
            lines.append(
                f"  • {sym} {sk}{dir_tag} "
                f"{pnl:+.1f}% 포지션=${pos_size:,.1f} ({held}/{cfg['hold_days']}일)"
            )
        except Exception:
            lines.append(
                f"  • {sym} {sk}{dir_tag} ${pos_size:,.1f} ({held}일)"
            )

    tg_send("\n".join(lines))


# ── 모니터링 (5분마다) ────────────────────────────────────────────────────────

def monitor():
    state = load_state()
    positions = state.get("positions", {})
    if not positions:
        return

    for sym in list(positions.keys()):
        pos = positions[sym]
        try:
            ticker = api.get_ticker(sym)
            cur_price = float(ticker["lastPrice"])
        except Exception as e:
            log.error(f"{sym} 모니터 가격 조회 실패: {e}")
            continue

        direction = pos["direction"]
        sl_price = pos["sl_price"]
        tp_price = pos["tp_price"]

        hit = False
        if direction == "long":
            if cur_price <= sl_price:
                close_pos(sym, state, f"SL ${cur_price:.4f}<=${sl_price:.4f}")
                hit = True
            elif cur_price >= tp_price:
                close_pos(sym, state, f"TP ${cur_price:.4f}>=${tp_price:.4f}")
                hit = True
        else:  # short
            if cur_price >= sl_price:
                close_pos(sym, state, f"SL ${cur_price:.4f}>=${sl_price:.4f}")
                hit = True
            elif cur_price <= tp_price:
                close_pos(sym, state, f"TP ${cur_price:.4f}<=${tp_price:.4f}")
                hit = True

        if hit:
            save_state(state)


# ── 상태 출력 ─────────────────────────────────────────────────────────────────

def print_status():
    state = load_state()
    positions = state.get("positions", {})
    log.info(f"--- 보유 포지션: {len(positions)}/{MAX_POS} ---")
    for sym, pos in positions.items():
        held = days_since(pos["entry_date"])
        cfg = STRATS[pos["strat"]]
        log.info(
            f"  {sym} {pos['strat']}({cfg['name']}) {pos['direction']} "
            f"entry=${pos['entry_price']:.4f} qty={pos['qty']} "
            f"SL=${pos['sl_price']:.4f} TP=${pos['tp_price']:.4f} "
            f"보유 {held}일/{cfg['hold_days']}일"
        )


# ── 테스트 모드 ───────────────────────────────────────────────────────────────

def run_test():
    """python bybit_main.py --test"""
    log.info("===== 바이비트 API 테스트 =====")

    log.info("[1] 잔고 조회")
    try:
        equity = api.get_equity()
        balance = api.get_balance()
        log.info(f"  총자산: ${equity:,.2f} | 가용: ${balance:,.2f}")
    except Exception as e:
        log.error(f"  잔고 조회 실패: {e}")

    log.info("[2] BTC 현재가")
    try:
        ticker = api.get_ticker("BTCUSDT")
        log.info(f"  BTC: ${float(ticker['lastPrice']):,.2f}")
    except Exception as e:
        log.error(f"  BTC 조회 실패: {e}")

    log.info("[3] BTC 시장 상태")
    is_bull = get_btc_market_state()
    log.info(f"  → {'강세' if is_bull else '약세'}")

    log.info("[4] 보유 포지션")
    try:
        positions = api.get_positions()
        log.info(f"  {len(positions)}개 포지션")
        for p in positions:
            log.info(f"    {p['symbol']} {p['side']} size={p['size']} entry=${p['entry_price']:.4f}")
    except Exception as e:
        log.error(f"  포지션 조회 실패: {e}")

    log.info("[5] 유니버스 상위 10")
    try:
        tickers = api.get_tickers_all()
        usdt = [t for t in tickers if t["symbol"].endswith("USDT")]
        ranked = sorted(usdt, key=lambda t: float(t.get("turnover24h", 0)), reverse=True)
        for i, t in enumerate(ranked[:10]):
            sym = t["symbol"]
            tv = float(t.get("turnover24h", 0))
            if sym in EXCLUDE:
                sym += " (제외)"
            log.info(f"    {i+1}. {sym} 거래대금=${tv:,.0f}")
    except Exception as e:
        log.error(f"  유니버스 조회 실패: {e}")

    log.info("===== 테스트 완료 =====")


# ── 드라이런 ──────────────────────────────────────────────────────────────────

def run_dry():
    """python bybit_main.py --dry  —  시그널 생성만 (주문 없음)"""
    global DRY_RUN
    DRY_RUN = True
    log.info("===== 드라이런 모드 =====")
    daily_check()
    print_status()


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    if not API_KEY or not API_SECRET:
        log.error("BYBIT_API_KEY / BYBIT_API_SECRET 환경변수 설정 필요")
        sys.exit(1)

    log.info("=" * 60)
    log.info("  바이비트 선물 자동매매 — 채널 돌파 복합 전략")
    log.info("=" * 60)
    log.info(f"  레버리지={LEVERAGE}x, 슬롯={MAX_POS}, 현금={CASH_RATIO*100:.0f}%")
    log.info(f"  테스트넷={'ON' if TESTNET else 'OFF'}")
    log.info(f"  드라이런={'ON' if DRY_RUN else 'OFF'}")
    for sk, cfg in STRATS.items():
        log.info(f"  {sk}: {cfg['name']} SL={cfg['sl']*100:.0f}% TP={cfg['tp']*100:.0f}% {cfg['hold_days']}일")

    # 시작 시 한 번 실행
    daily_check()
    print_status()

    # 스케줄 등록 (UTC) — 백테스트와 동일하게 하루 1회
    schedule.every().day.at("00:05").do(daily_check)
    schedule.every().day.at("00:10").do(print_status)

    log.info("스케줄:")
    log.info("  00:05 UTC → 일간 체크 (시그널 + 진입/청산 + SL/TP)")

    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            log.error(f"루프 오류: {e}")
        time.sleep(10)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "--test":
            run_test()
        elif sys.argv[1] == "--dry":
            run_dry()
        elif sys.argv[1] == "--status":
            print_status()
        else:
            print("사용법: python bybit_main.py [--test|--dry|--status]")
    else:
        main()
