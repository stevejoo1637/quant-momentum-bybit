#!/usr/bin/env python3
"""
바이비트 선물 채널 돌파 복합 전략 자동매매 v2
==============================================
v1 대비 변경:
  - 포지션 사이징: 고정 1/4 → 동적 1/n (n = 보유 포지션 수)
  - 포지션 리사이즈: 진입/청산 시 기존 포지션 비중 자동 조정
  - 월간 리밸런싱: 매월 말 1/n 비중 재조정
  - 리사이즈 간 60초 대기 (안정성)

전략:
  A: 상단돌파 롱 (강세, SL-7%/TP+25%/7일, R²>0.5, 볼륨1.5x)
  B: 하단돌파 롱 (무관, SL-5%/TP+15%/14일, R²>0.5, 볼륨1.0x)
  C: 상단터치 숏 (약세, SL-10%/TP+20%/10일, R²>0.3, 볼륨1.0x)

장중 안전장치 (5분 모니터링):
  B/C: max_loss -15% (장중 스톱)
  B: max_profit +30% (장중 TP)
  A: 장중 제한 없음

설정:
  레버리지: 2x | 슬롯: 4 | 현금비율: 30%
  시장필터: BTC SMA20 > SMA50
  유니버스: 거래대금 상위 60 (BTC/ETH 제외, 상장 150일 미만 제외)

일정 (24/7):
  00:05 UTC → 일간 체크 (시그널 생성 + 진입/청산 + 리사이즈)
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
import db_logger

from bybit_api import BybitAPI

# ── 설정 ─────────────────────────────────────────────────────────────────────

API_KEY    = os.environ.get("BYBIT_API_KEY", "")
API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
TESTNET    = os.environ.get("BYBIT_TESTNET", "0") == "1"

LEVERAGE   = 2
MAX_POS    = 4
CASH_RATIO = 0.30
MDD_DEPLOY_THRESH = -0.35  # MDD -35% 도달 시 현금 전량투입
TOP_N      = 60
MIN_LIST_DAYS = 150  # 상장 150일 미만 종목 제외
EXCLUDE    = {"BTCUSDT", "ETHUSDT"}

RESIZE_MIN_DELTA_USDT = 5.0   # $5 미만 리사이즈 차이는 무시
RESIZE_WAIT_SEC       = 60    # 리사이즈 간 대기 시간 (초)

# 장중 안전장치 (5분 모니터링)
INTRADAY_MAX_LOSS  = {"A": None, "B": -0.15, "C": -0.15}
INTRADAY_MAX_PROFIT = {"A": None, "B": 0.30, "C": None}

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
        "sl": 0.10, "tp": 0.20, "hold_days": 10,
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


def get_effective_cash_ratio(state: dict, equity: float) -> float:
    """MDD 기반 실효 현금비율 계산"""
    peak = state.get("peak_equity", equity)
    if equity > peak:
        state["peak_equity"] = equity
        peak = equity
        state["mdd_deployed"] = False

    current_mdd = (equity / peak - 1) if peak > 0 else 0
    mdd_deployed = state.get("mdd_deployed", False)

    if current_mdd <= MDD_DEPLOY_THRESH and not mdd_deployed:
        state["mdd_deployed"] = True
        save_state(state)
        log.info(f"MDD {current_mdd*100:.1f}% → 현금 전량투입!")
        tg_send(f"⚠️ MDD {current_mdd*100:.1f}% 도달 → 현금 전량투입")
        return 0.0
    elif mdd_deployed:
        return 0.0
    else:
        return CASH_RATIO


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

        prev_year = str(int(current_year) - 1)
        start_ms = int(pd.Timestamp(f"{prev_year}-01-01").timestamp() * 1000)
        end_ms = int(pd.Timestamp(f"{prev_year}-12-31").timestamp() * 1000)

        avg_turnover = {}
        for i, sym in enumerate(candidates):
            try:
                all_klines = []
                fetch_end = end_ms
                for _ in range(3):
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


# ── 포지션 리사이즈 (핵심 신규 기능) ─────────────────────────────────────────

def resize_positions(state: dict, instruments: dict, target_n: int, reason: str = "리사이즈"):
    """
    기존 포지션을 1/target_n 비중으로 리사이즈.
    - 비중 증가 시: 같은 방향으로 추가 주문
    - 비중 감소 시: 부분 청산 (reduceOnly)
    - 각 리사이즈 사이 60초 대기
    """
    positions = state["positions"]
    if not positions or target_n <= 0:
        return

    # 최신 자산 조회
    try:
        equity = api.get_equity()
    except Exception as e:
        log.error(f"리사이즈 자산 조회 실패: {e}")
        return

    effective_cash_ratio = get_effective_cash_ratio(state, equity)
    invest_capital = equity * (1 - effective_cash_ratio)
    target_slot_usdt = invest_capital / target_n
    target_order_usdt = target_slot_usdt * LEVERAGE

    log.info(f"── 리사이즈: 1/{target_n} 비중 (슬롯 ${target_slot_usdt:,.0f}, 주문 ${target_order_usdt:,.0f}) [{reason}] ──")

    resized_syms = []
    for sym in list(positions.keys()):
        pos = positions[sym]

        try:
            ticker = api.get_ticker(sym)
            cur_price = float(ticker["lastPrice"])
        except Exception as e:
            log.error(f"  {sym} 가격 조회 실패: {e}")
            continue

        current_qty = pos["qty"]
        current_usdt = current_qty * cur_price
        target_qty = target_order_usdt / cur_price

        inst = instruments.get(sym, {})
        qty_step = inst.get("qty_step", 0.001)
        min_qty = inst.get("min_qty", 0.001)

        delta_qty = target_qty - current_qty
        delta_usdt = abs(delta_qty) * cur_price

        if delta_usdt < RESIZE_MIN_DELTA_USDT:
            log.info(f"  {sym}: 차이 ${delta_usdt:.1f} < ${RESIZE_MIN_DELTA_USDT} → 스킵")
            continue

        if DRY_RUN:
            new_qty = target_qty
            log.info(f"  [DRY] {sym}: {current_qty:.4f} → {new_qty:.4f} (${current_usdt:.0f} → ${target_order_usdt:.0f})")
            pos["qty"] = new_qty
            resized_syms.append(sym)
            continue

        try:
            if delta_qty > 0:
                # ── 비중 증가: 같은 방향 추가 주문 ──
                add_qty_str = round_qty(delta_qty, qty_step)
                if float(add_qty_str) < min_qty:
                    log.warning(f"  {sym}: 추가수량 {add_qty_str} < 최소 {min_qty} → 스킵")
                    continue

                if pos["direction"] == "long":
                    api.open_long(sym, add_qty_str)
                else:
                    api.open_short(sym, add_qty_str)

                new_qty = current_qty + float(add_qty_str)
                log.info(f"  {sym}: +{add_qty_str} (${current_usdt:.0f} → ${new_qty * cur_price:.0f})")
            else:
                # ── 비중 감소: 부분 청산 ──
                reduce_qty = abs(delta_qty)
                reduce_qty_str = round_qty(reduce_qty, qty_step)
                if float(reduce_qty_str) < min_qty:
                    log.warning(f"  {sym}: 축소수량 {reduce_qty_str} < 최소 {min_qty} → 스킵")
                    continue

                api.close_position(sym, pos["side"], reduce_qty_str)

                new_qty = current_qty - float(reduce_qty_str)
                log.info(f"  {sym}: -{reduce_qty_str} (${current_usdt:.0f} → ${new_qty * cur_price:.0f})")

            pos["qty"] = new_qty
            resized_syms.append(sym)
            save_state(state)

            # 다음 리사이즈 전 대기
            log.info(f"  {RESIZE_WAIT_SEC}초 대기...")
            time.sleep(RESIZE_WAIT_SEC)

        except Exception as e:
            log.error(f"  {sym} 리사이즈 실패: {e}")

    if resized_syms:
        tg_send(
            f"🔄 <b>포지션 리사이즈</b> [{reason}]\n"
            f"비중: 1/{target_n} | {len(resized_syms)}종목\n"
            f"슬롯당 ${target_order_usdt:,.0f}\n"
            f"종목: {', '.join(resized_syms)}"
        )

    save_state(state)
    log.info(f"  리사이즈 완료: {len(resized_syms)}종목")


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

    reason_kr = reason
    if reason.startswith("BTC필터"):
        btcf = reason.split("(")[-1].rstrip(")")
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

    # 가상 누적수익률: 1/n 비중 (청산 직전 포지션 수 기준)
    n_pos = len(state["positions"])
    weight = 1.0 / n_pos if n_pos > 0 else 1.0
    weighted_pnl = pnl * weight
    trade_log = state.setdefault("trade_log", [])
    trade_log.append({
        "symbol": symbol, "strat": sk, "dir": direction,
        "entry": entry, "exit": cur_price, "pnl": pnl,
        "wpnl": weighted_pnl, "reason": reason, "held": held,
        "date": today_str(), "n_pos": n_pos,
    })

    tg_send(
        f"{pnl_emoji} <b>청산</b> {symbol}\n"
        f"{sk}{dir_tag} | {reason_kr}\n"
        f"${entry:.4f} → ${cur_price:.4f}\n"
        f"수익: <b>{pnl:+.1f}%</b> ({held}일) | 비중: 1/{n_pos}"
    )
    # DB 로깅
    try:
        db_logger.log_trade(
            symbol=symbol, side=side, entry_price=entry, exit_price=cur_price,
            qty=float(qty), pnl=pnl, pnl_pct=pnl,
            strategy=sk, reason=reason, hold_days=held
        )
        db_logger.remove_position(symbol)
    except Exception as e:
        log.warning(f"DB 로깅 실패: {e}")

    del state["positions"][symbol]


# ── 일간 체크 (00:05 UTC) ────────────────────────────────────────────────────

def daily_check():
    log.info("=" * 60)
    log.info("  일간 체크 시작 (v2: weight=1/n)")
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

    # ─────────────────────────────────────────────────────────
    # 4. 청산 단계
    # ─────────────────────────────────────────────────────────
    n_before_close = len(state["positions"])

    # 4a. BTC 필터 청산
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

    # 4b. SL/TP/TIME 청산
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

    n_after_close = len(state["positions"])
    closed_count = n_before_close - n_after_close
    if closed_count > 0:
        log.info(f"청산: {closed_count}개 → 잔여 {n_after_close}개")

    # ─────────────────────────────────────────────────────────
    # 5. 후보 스캔 (진입 전에 먼저 스캔하여 final_n 결정)
    # ─────────────────────────────────────────────────────────
    avail_slots = MAX_POS - n_after_close
    candidates = []
    held_symbols = set(state["positions"].keys())
    # 유니버스 순위 (거래대금 순) — 유저코드 tiebreaker용
    universe_rank = {sym: i for i, sym in enumerate(universe)}

    if avail_slots > 0:
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

                if len(closes) >= 6:
                    mom5 = curr_close / closes[-6] - 1
                else:
                    mom5 = 0.01

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
                        rank = universe_rank.get(sym, 999)
                        candidates.append((sym, sk, score, rank))

            except Exception as e:
                log.debug(f"{sym} 스캔 실패: {e}")
                continue

            time.sleep(0.1)

    # 점수순 → 거래대금순 정렬 (유저코드 동일: -score, rank)
    candidates.sort(key=lambda x: (-x[2], x[3]))
    selected = []
    selected_syms = set(held_symbols)
    for sym, sk, score, rank in candidates:
        if len(selected) >= avail_slots:
            break
        if sym in selected_syms:
            continue
        selected.append((sym, sk, score))
        selected_syms.add(sym)

    new_entries_count = len(selected)
    log.info(f"시그널 후보: {len(candidates)}개 → 진입 예정: {new_entries_count}개")

    # ─────────────────────────────────────────────────────────
    # 6. 리사이즈 단계
    # ─────────────────────────────────────────────────────────
    final_n = n_after_close + new_entries_count

    if final_n > 0 and n_after_close > 0:
        # 포지션 수 변화가 있으면 리사이즈
        if closed_count > 0 or new_entries_count > 0:
            resize_positions(
                state, instruments, final_n,
                reason=f"청산{closed_count}/진입{new_entries_count} → 1/{final_n}"
            )

    # ─────────────────────────────────────────────────────────
    # 7. 신규 진입
    # ─────────────────────────────────────────────────────────
    if new_entries_count > 0:
        # 최신 자산 조회 (리사이즈 후 변동)
        try:
            equity = api.get_equity()
            log.info(f"총 자산: ${equity:,.2f}")
        except Exception as e:
            log.error(f"자산 조회 실패: {e}")
            return

        effective_cash_ratio = get_effective_cash_ratio(state, equity)
        invest_capital = equity * (1 - effective_cash_ratio)
        per_slot = invest_capital / final_n  # 1/n 비중
        order_usdt = per_slot * LEVERAGE

        log.info(f"진입 사이징: 1/{final_n} 비중, 슬롯 ${per_slot:,.0f}, 주문 ${order_usdt:,.0f}")

        for sym, sk, score in selected:
            if len(state["positions"]) >= MAX_POS:
                break

            cfg = STRATS[sk]
            direction = cfg["direction"]

            try:
                ticker = api.get_ticker(sym)
                cur_price = float(ticker["lastPrice"])
                if cur_price <= 0:
                    continue

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
                try:
                    db_logger.upsert_position(
                        symbol=sym, side=side, entry_price=cur_price,
                        qty=float(qty_str), sl_price=sl_price, tp_price=tp_price,
                        strategy=sk, entry_time=today_str()
                    )
                except Exception as e:
                    log.warning(f"DB 포지션 기록 실패: {e}")

                pos_value = float(qty_str) * cur_price
                n_now = len(state["positions"])
                log.info(
                    f"진입: {sym} {sk}({cfg['name']}) {direction} "
                    f"qty={qty_str} @ ${cur_price:.4f} "
                    f"SL=${sl_price:.4f} TP=${tp_price:.4f} "
                    f"(비중 1/{n_now})"
                )
                dir_tag = "숏" if direction == "short" else "롱"
                tg_send(
                    f"🟢 <b>진입</b> {sym}\n"
                    f"{sk}{dir_tag} | ${cur_price:.4f} × {qty_str} (${pos_value:,.1f})\n"
                    f"손절: ${sl_price:.4f} (-{cfg['sl']*100:.0f}%)\n"
                    f"익절: ${tp_price:.4f} (+{cfg['tp']*100:.0f}%)\n"
                    f"보유한도: {cfg['hold_days']}일 | 비중: 1/{final_n}"
                )

                save_state(state)

                # 다음 진입 전 대기
                log.info(f"  {RESIZE_WAIT_SEC}초 대기...")
                time.sleep(RESIZE_WAIT_SEC)

            except Exception as e:
                log.error(f"{sym} 진입 실패: {e}")

    # ─────────────────────────────────────────────────────────
    # 8. 월간 리밸런싱 (유저코드 동일: 월말에 1/n 재조정 + 현금비중 복구)
    #    - 포지션 변동 없는 달에도 drift 보정
    #    - MDD 복구 후 현금 40% 복구 역할
    #    - MDD 투입 상태(mdd_deployed)면 스킵
    # ─────────────────────────────────────────────────────────
    today = now_utc()
    last_rebal = state.get("last_rebal_month", "")
    current_month = today.strftime("%Y-%m")

    mdd_deployed = state.get("mdd_deployed", False)
    if (last_rebal != current_month
            and len(state["positions"]) > 0
            and closed_count == 0 and new_entries_count == 0
            and not mdd_deployed):
        log.info(f"월간 리밸런싱: {current_month}")
        n_current = len(state["positions"])
        resize_positions(state, instruments, n_current, reason=f"월간리밸런싱({current_month})")
        state["last_rebal_month"] = current_month
        save_state(state)

    save_state(state)
    log.info(f"보유: {len(state['positions'])}포지션")
    log.info("=" * 60)

    # 텔레그램 일간 리포트
    try:
        equity = api.get_equity()
    except Exception:
        equity = 0
    send_daily_report(state, is_bull, equity)

    # DB 일일 성과 기록
    try:
        today_trades = [t for t in state.get("trade_log", []) if t.get("date") == today_str()]
        win_count = sum(1 for t in today_trades if t.get("pnl", 0) > 0)
        prev_equity = state.get("prev_equity", equity)
        daily_pnl = equity - prev_equity
        daily_pnl_pct = (daily_pnl / prev_equity * 100) if prev_equity > 0 else 0
        btc_state_str = "bull" if is_bull else "bear"
        try:
            btc_price = float(api.get_ticker("BTCUSDT")["lastPrice"])
        except Exception:
            btc_price = 0
        db_logger.log_daily(
            date_str=today_str(), equity=equity, daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct, open_positions=len(state["positions"]),
            total_trades=len(today_trades), win_trades=win_count,
            btc_price=btc_price, btc_state=btc_state_str
        )
        state["prev_equity"] = equity
        save_state(state)
    except Exception as e:
        log.warning(f"DB 일일성과 기록 실패: {e}")


# ── 일간 리포트 ───────────────────────────────────────────────────────────────

def send_daily_report(state: dict, is_bull: bool, equity: float):
    """텔레그램 일간 리포트 — 1/n 비중 기반 가상 누적수익률"""
    positions = state.get("positions", {})
    trade_log = state.get("trade_log", [])

    if "start_date" not in state:
        state["start_date"] = today_str()
        save_state(state)

    # ── 가상 누적수익률 (복리, 1/n 비중) ──
    nav = 1.0
    peak_nav = 1.0
    mdd = 0.0
    for t in trade_log:
        n_pos = t.get("n_pos", MAX_POS)  # v2: 실제 포지션 수, v1 호환: MAX_POS
        weight = 1.0 / n_pos if n_pos > 0 else 1.0 / MAX_POS
        pnl_pct = t.get("pnl", 0) / 100
        nav *= (1 + pnl_pct * weight)
        if nav > peak_nav:
            peak_nav = nav
        dd = (nav / peak_nav - 1) * 100
        if dd < mdd:
            mdd = dd

    # 미체결 포지션 평가손익 반영
    open_nav = nav
    n_open = len(positions)
    for sym, pos in positions.items():
        try:
            ticker = api.get_ticker(sym)
            cur = float(ticker["lastPrice"])
            entry_p = pos["entry_price"]
            if pos["direction"] == "short":
                p = -(cur / entry_p - 1)
            else:
                p = cur / entry_p - 1
            weight = 1.0 / n_open if n_open > 0 else 1.0
            open_nav *= (1 + p * weight)
        except Exception:
            pass

    total_ret = (open_nav - 1) * 100
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
        f"📊 <b>일간 리포트</b> {today_str()} (v2: 1/n)",
        f"BTC 시장: {market}",
        f"총자산: <b>${equity:,.2f}</b>",
        f"누적수익률: <b>{total_ret:+.1f}%</b> | CAGR: {cagr:+.1f}%",
        f"MDD: {mdd_str}",
        f"거래: {n_trades}건 (승률 {wr:.0f}%)",
        f"",
        f"보유: {n_open}/{MAX_POS}슬롯 (비중: 1/{n_open})" if n_open > 0 else f"보유: 0/{MAX_POS}슬롯",
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


# ── 모니터링 (5분마다) — 장중 안전장치 ────────────────────────────────────────

def monitor():
    """5분마다 장중 max_loss/max_profit 체크 (유저코드 v2)"""
    state = load_state()
    positions = state.get("positions", {})
    if not positions:
        return

    for sym in list(positions.keys()):
        pos = positions[sym]
        sk = pos["strat"]
        try:
            ticker = api.get_ticker(sym)
            cur_price = float(ticker["lastPrice"])
        except Exception as e:
            log.error(f"{sym} 모니터 가격 조회 실패: {e}")
            continue

        entry = pos["entry_price"]
        direction = pos["direction"]

        if direction == "short":
            pnl = -(cur_price / entry - 1)
        else:
            pnl = cur_price / entry - 1

        hit = False

        # 장중 max_loss (B/C: -15%)
        ml = INTRADAY_MAX_LOSS.get(sk)
        if ml is not None and pnl <= ml:
            close_pos(sym, state, f"MAXLOSS {pnl*100:+.1f}% (한도{ml*100:.0f}%)")
            hit = True

        # 장중 max_profit (B: +30%)
        if not hit:
            mp = INTRADAY_MAX_PROFIT.get(sk)
            if mp is not None and pnl >= mp:
                close_pos(sym, state, f"MAXPROFIT {pnl*100:+.1f}% (한도+{mp*100:.0f}%)")
                hit = True

        if hit:
            save_state(state)


# ── 상태 출력 ─────────────────────────────────────────────────────────────────

def print_status():
    state = load_state()
    positions = state.get("positions", {})
    n = len(positions)
    log.info(f"--- 보유 포지션: {n}/{MAX_POS} (비중: 1/{n}) ---" if n > 0
             else f"--- 보유 포지션: 0/{MAX_POS} ---")
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
    log.info("  바이비트 선물 자동매매 v2 — 채널 돌파 (weight=1/n)")
    log.info("=" * 60)
    log.info(f"  레버리지={LEVERAGE}x, 슬롯={MAX_POS}, 현금={CASH_RATIO*100:.0f}%")
    log.info(f"  포지션 사이징: 동적 1/n (최대 {MAX_POS}슬롯)")
    log.info(f"  월간 리밸런싱: ON")
    log.info(f"  테스트넷={'ON' if TESTNET else 'OFF'}")
    log.info(f"  드라이런={'ON' if DRY_RUN else 'OFF'}")
    for sk, cfg in STRATS.items():
        ml = INTRADAY_MAX_LOSS.get(sk)
        mp = INTRADAY_MAX_PROFIT.get(sk)
        ml_s = f"maxloss{ml*100:.0f}%" if ml else "-"
        mp_s = f"maxprofit+{mp*100:.0f}%" if mp else "-"
        log.info(f"  {sk}: {cfg['name']} SL={cfg['sl']*100:.0f}% TP={cfg['tp']*100:.0f}% {cfg['hold_days']}일 [{ml_s}/{mp_s}]")

    # 시작 시 상태만 출력 (daily_check는 00:05 UTC에만 실행)
    print_status()

    # 스케줄 등록
    schedule.every().day.at("00:05").do(daily_check)
    schedule.every().day.at("00:10").do(print_status)
    schedule.every(5).minutes.do(monitor)  # 장중 안전장치

    log.info("스케줄:")
    log.info("  00:05 UTC → 일간 체크 (시그널 + 진입/청산 + 리사이즈)")
    log.info("  5분마다 → 장중 안전장치 (B/C maxloss-15%, B maxprofit+30%)")

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
