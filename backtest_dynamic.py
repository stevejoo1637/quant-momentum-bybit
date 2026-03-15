#!/usr/bin/env python3
"""
바이비트 채널 돌파 복합 전략 백테스트
====================================
VPS bybit_main.py 100% 동일 로직 재현:
  - 채널: 선형회귀 20일, 2σ
  - A: 상단돌파 롱 (강세, SL7%, TP25%, 7일, R²>0.5, vol1.5x)
  - B: 하단돌파 롱 (무관, SL5%, TP15%, 14일, R²>0.5, vol1.0x)
  - C: 상단터치 숏 (약세, SL10%, TP20%, 10일, R²>0.3, vol1.0x)
  - BTC 필터: SMA20 > SMA50
  - 유니버스: 전년 평균 거래대금 상위 60 (BTC/ETH 제외, 상장150일 미만 제외)
  - 포지션: 동적 1/n 비중 (리사이징), 레버리지 3x, 현금 30%
  - 장중 안전장치: B/C max_loss -15%, B max_profit +30%
  - MDD -35% 도달 시 현금 전량투입
"""

import os
import sys
import time
import math
import pickle
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from pybit.unified_trading import HTTP

# ── 설정 (VPS 동일) ─────────────────────────────────────────────────────────

LEVERAGE   = 3
MAX_POS    = 4
CASH_RATIO = 0.30
MDD_DEPLOY_THRESH = -0.35
TOP_N      = 60
MIN_LIST_DAYS = 150
EXCLUDE    = {"BTCUSDT", "ETHUSDT"}

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

INITIAL_CAPITAL = 10000.0  # 백테스트 시작 자본 $10,000

# ── 백테스트 기간 ───────────────────────────────────────────────────────────

BT_START = "2023-01-01"
BT_END   = "2026-03-13"

CACHE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(CACHE_DIR, "bt_cache.pkl")

# ── 로깅 ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── API ─────────────────────────────────────────────────────────────────────

session = HTTP()  # 공개 데이터만 사용 (인증 불필요)


# ── 데이터 다운로드 ─────────────────────────────────────────────────────────

def download_klines(symbol: str, start_date: str, end_date: str,
                    interval: str = "D") -> pd.DataFrame:
    """바이비트에서 일봉 데이터 다운로드 (end에서 역방향 페이징)"""
    start_ms = int(pd.Timestamp(start_date).timestamp() * 1000)
    end_ms = int(pd.Timestamp(end_date).timestamp() * 1000)

    all_rows = []
    fetch_end = end_ms

    for _ in range(20):  # 최대 20번 페이징
        try:
            resp = session.get_kline(
                category="linear", symbol=symbol,
                interval=interval, limit=200,
                start=start_ms, end=fetch_end,
            )
            rows = resp["result"]["list"]
            if not rows:
                break
            all_rows.extend(rows)
            # 바이비트는 최신순 반환 → 마지막이 가장 오래된 것
            earliest_ts = min(int(r[0]) for r in rows)
            if earliest_ts <= start_ms:
                break
            fetch_end = earliest_ts - 1
            time.sleep(0.05)
        except Exception as e:
            log.warning(f"  {symbol} 다운로드 실패: {e}")
            break

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
    df["ts"] = df["ts"].astype(int)
    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        df[col] = df[col].astype(float)
    df["date"] = pd.to_datetime(df["ts"], unit="ms").dt.strftime("%Y-%m-%d")
    df = df.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
    return df


def get_universe_symbols() -> list[str]:
    """거래 가능한 USDT 퍼페추얼 종목 목록 (상장일 필터 포함)"""
    instruments = session.get_instruments_info(category="linear")
    now_ms = int(time.time() * 1000)
    min_launch_ms = now_ms - MIN_LIST_DAYS * 86400 * 1000

    symbols = []
    for item in instruments["result"]["list"]:
        sym = item["symbol"]
        if not sym.endswith("USDT") or sym in EXCLUDE:
            continue
        if item.get("status") != "Trading":
            continue
        lt = int(item.get("launchTime", "0") or "0")
        if lt > min_launch_ms:
            continue
        symbols.append(sym)
    return symbols


def download_all_data(symbols: list[str], start_date: str, end_date: str) -> dict:
    """모든 종목 + BTC 일봉 데이터 다운로드 (캐시 사용)"""
    if os.path.exists(CACHE_FILE):
        log.info(f"캐시 로드: {CACHE_FILE}")
        with open(CACHE_FILE, "rb") as f:
            cache = pickle.load(f)
        # 캐시에 있는 종목 확인
        cached_syms = set(cache.keys())
        missing = [s for s in symbols if s not in cached_syms]
        if "BTCUSDT" not in cached_syms:
            missing.append("BTCUSDT")
        if not missing:
            log.info(f"캐시 완전 적중: {len(cache)}종목")
            return cache
        log.info(f"추가 다운로드 필요: {len(missing)}종목")
    else:
        cache = {}
        missing = list(symbols) + ["BTCUSDT"]

    # 다운로드 시작일: 전년 유니버스 계산 위해 1년+90일 여유
    dl_start = (pd.Timestamp(start_date) - pd.Timedelta(days=455)).strftime("%Y-%m-%d")

    total = len(missing)
    for i, sym in enumerate(missing):
        if (i + 1) % 10 == 0 or i == 0:
            log.info(f"  다운로드: {i+1}/{total} ({sym})")
        df = download_klines(sym, dl_start, end_date)
        if not df.empty:
            cache[sym] = df
        time.sleep(0.08)

    # 캐시 저장
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(cache, f)
    log.info(f"캐시 저장: {len(cache)}종목 → {CACHE_FILE}")
    return cache


# ── 유니버스 선정 (VPS 동일: 전년 평균 거래대금) ────────────────────────────

def select_universe_for_year(data: dict, year: int, all_symbols: list[str]) -> list[str]:
    """전년도 평균 거래대금 상위 TOP_N 종목 (전년 데이터 부족 시 당해 대체)"""
    prev_year = str(year - 1)
    avg_turnover = {}

    for sym in all_symbols:
        if sym in EXCLUDE or sym == "BTCUSDT":
            continue
        df = data.get(sym)
        if df is None or df.empty:
            continue
        # 전년도 데이터
        mask = df["date"].str.startswith(prev_year)
        prev_df = df[mask]
        if len(prev_df) < 100:
            # 전년 데이터 부족 시 당해 1월 이전 가용 데이터 사용
            fallback_end = f"{year}-01-01"
            fb_df = df[df["date"] < fallback_end]
            if len(fb_df) < 50:
                continue
            tv = (fb_df["close"] * fb_df["volume"]).mean()
        else:
            tv = (prev_df["close"] * prev_df["volume"]).mean()
        avg_turnover[sym] = tv

    ranked = sorted(avg_turnover.items(), key=lambda x: -x[1])
    universe = [sym for sym, _ in ranked[:TOP_N]]
    return universe


# ── 채널 계산 (VPS 100% 동일) ──────────────────────────────────────────────

def calc_channel(closes: list[float]) -> dict | None:
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


# ── BTC 시장 필터 (VPS 동일) ───────────────────────────────────────────────

def get_btc_state(btc_df: pd.DataFrame, date: str) -> bool | None:
    """BTC SMA20 > SMA50 → True(강세)"""
    mask = btc_df["date"] <= date
    closes = btc_df.loc[mask, "close"].values
    if len(closes) < 50:
        return None
    sma20 = np.mean(closes[-20:])
    sma50 = np.mean(closes[-50:])
    return sma20 > sma50


# ── 백테스트 엔진 ──────────────────────────────────────────────────────────

def run_backtest(data: dict, all_symbols: list[str]):
    """VPS 로직 100% 재현 백테스트"""

    btc_df = data.get("BTCUSDT")
    if btc_df is None:
        log.error("BTC 데이터 없음")
        return

    # 거래일 목록
    all_dates = sorted(btc_df[btc_df["date"] >= BT_START]["date"].unique())
    all_dates = [d for d in all_dates if d <= BT_END]
    log.info(f"백테스트: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)}일)")

    # 상태 — 라이브와 동일하게 equity를 직접 추적
    cash = INITIAL_CAPITAL        # 현금 (주문에 사용되지 않은 잔액)
    peak_equity = INITIAL_CAPITAL
    mdd_deployed = False
    positions = {}   # sym -> {strat, direction, entry_price, entry_date, side, qty_usdt, margin}
    trade_log = []
    equity_curve = []
    universe = []
    current_universe_year = None

    def get_equity_now(today_date):
        """라이브 api.get_equity()와 동일: 현금 + 미실현 포지션 평가"""
        eq = cash
        for s, p in positions.items():
            sdf = data.get(s)
            if sdf is None:
                continue
            r = sdf[sdf["date"] == today_date]
            if r.empty:
                eq += p["margin"]  # 가격 없으면 원금만
                continue
            cur = r.iloc[0]["close"]
            entry_p = p["entry_price"]
            if p["direction"] == "short":
                pnl_pct = -(cur / entry_p - 1)
            else:
                pnl_pct = cur / entry_p - 1
            # 마진 + 레버리지 수익
            eq += p["margin"] + p["qty_usdt"] * pnl_pct
        return eq

    for day_idx, today in enumerate(all_dates):
        year = int(today[:4])

        # ── 유니버스 갱신 (연 1회) ──
        if current_universe_year != year:
            universe = select_universe_for_year(data, year, all_symbols)
            current_universe_year = year
            if day_idx == 0 or year != int(all_dates[day_idx-1][:4]):
                log.info(f"[{today}] 유니버스 갱신: {len(universe)}종목 (상위: {universe[:5]})")

        # ── BTC 시장 상태 ──
        is_bull = get_btc_state(btc_df, today)
        if is_bull is None:
            equity = get_equity_now(today)
            equity_curve.append({"date": today, "equity": equity})
            continue

        # ── 현재 자산 ──
        equity = get_equity_now(today)

        # ── MDD 기반 현금비율 ──
        if equity > peak_equity:
            peak_equity = equity
            mdd_deployed = False
        current_mdd = (equity / peak_equity - 1) if peak_equity > 0 else 0
        if current_mdd <= MDD_DEPLOY_THRESH and not mdd_deployed:
            mdd_deployed = True
            log.info(f"[{today}] MDD {current_mdd*100:.1f}% → 현금 전량투입!")
        effective_cash_ratio = 0.0 if mdd_deployed else CASH_RATIO

        # ── 장중 안전장치 (high/low로 시뮬레이션) ──
        for sym in list(positions.keys()):
            pos = positions[sym]
            sym_df = data.get(sym)
            if sym_df is None:
                continue
            row = sym_df[sym_df["date"] == today]
            if row.empty:
                continue
            row = row.iloc[0]
            entry = pos["entry_price"]
            sk = pos["strat"]

            if pos["direction"] == "long":
                worst_pnl = row["low"] / entry - 1
                best_pnl = row["high"] / entry - 1
            else:
                worst_pnl = -(row["high"] / entry - 1)
                best_pnl = -(row["low"] / entry - 1)

            # max_loss 체크
            ml = INTRADAY_MAX_LOSS.get(sk)
            if ml is not None and worst_pnl <= ml:
                # 청산: 마진 + 실현손익을 현금에 반환
                realized = pos["qty_usdt"] * ml
                cash += pos["margin"] + realized
                _close_position(positions, pos, sym, ml, today, "MAXLOSS", trade_log)
                continue

            # max_profit 체크
            mp = INTRADAY_MAX_PROFIT.get(sk)
            if mp is not None and best_pnl >= mp:
                realized = pos["qty_usdt"] * mp
                cash += pos["margin"] + realized
                _close_position(positions, pos, sym, mp, today, "MAXPROFIT", trade_log)
                continue

        # ── 청산 단계 (일봉 종가 기준) ──
        n_before = len(positions)

        # BTC 필터 청산
        for sym in list(positions.keys()):
            pos = positions[sym]
            sk = pos["strat"]
            btcf = STRATS[sk]["btc_filter"]
            if (btcf == "bull" and not is_bull) or (btcf == "bear" and is_bull):
                sym_df = data.get(sym)
                if sym_df is None:
                    continue
                row = sym_df[sym_df["date"] == today]
                if row.empty:
                    continue
                cur_price = row.iloc[0]["close"]
                if pos["direction"] == "short":
                    pnl = -(cur_price / pos["entry_price"] - 1)
                else:
                    pnl = cur_price / pos["entry_price"] - 1
                realized = pos["qty_usdt"] * pnl
                cash += pos["margin"] + realized
                _close_position(positions, pos, sym, pnl, today, f"BTC필터({btcf})", trade_log)

        # SL/TP/TIME 청산
        for sym in list(positions.keys()):
            pos = positions[sym]
            sym_df = data.get(sym)
            if sym_df is None:
                continue
            row = sym_df[sym_df["date"] == today]
            if row.empty:
                continue

            cur_price = row.iloc[0]["close"]
            entry = pos["entry_price"]
            if pos["direction"] == "short":
                pnl = -(cur_price / entry - 1)
            else:
                pnl = cur_price / entry - 1

            entry_dt = datetime.strptime(pos["entry_date"], "%Y-%m-%d")
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            held = (today_dt - entry_dt).days

            sk = pos["strat"]
            cfg = STRATS[sk]

            if pnl <= -cfg["sl"]:
                realized = pos["qty_usdt"] * pnl
                cash += pos["margin"] + realized
                _close_position(positions, pos, sym, pnl, today, f"SL", trade_log)
            elif pnl >= cfg["tp"]:
                realized = pos["qty_usdt"] * pnl
                cash += pos["margin"] + realized
                _close_position(positions, pos, sym, pnl, today, f"TP", trade_log)
            elif held >= cfg["hold_days"]:
                realized = pos["qty_usdt"] * pnl
                cash += pos["margin"] + realized
                _close_position(positions, pos, sym, pnl, today, f"TIME", trade_log)

        # ── 신규 진입 ──
        equity = get_equity_now(today)  # 청산 후 재계산
        avail_slots = MAX_POS - len(positions)
        candidates = []
        held_symbols = set(positions.keys())
        universe_rank = {sym: i for i, sym in enumerate(universe)}

        if avail_slots > 0:
            for sym in universe:
                if sym in held_symbols:
                    continue
                sym_df = data.get(sym)
                if sym_df is None:
                    continue

                mask = sym_df["date"] <= today
                df_up_to = sym_df[mask]
                if len(df_up_to) < CHANNEL_PERIOD + 1:
                    continue

                closes = df_up_to["close"].values.tolist()
                volumes = df_up_to["volume"].values.tolist()

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
                        candidates.append((sym, sk, score, rank, curr_close))

        # 정렬 (VPS 동일: -score, rank)
        candidates.sort(key=lambda x: (-x[2], x[3]))
        selected = []
        selected_syms = set(held_symbols)
        for sym, sk, score, rank, price in candidates:
            if len(selected) >= avail_slots:
                break
            if sym in selected_syms:
                continue
            selected.append((sym, sk, score, price))
            selected_syms.add(sym)

        # 진입 실행 — 동적 1/n: 기존 포지션 리사이징 + 신규 진입
        if selected:
            n_after = min(len(positions) + len(selected), MAX_POS)
            invest_capital = equity * (1 - effective_cash_ratio)
            per_slot = invest_capital / n_after  # 동적 1/n 비중

            # 기존 포지션 리사이징 (축소)
            for sym_r, pos_r in list(positions.items()):
                old_margin = pos_r["margin"]
                new_margin = per_slot
                new_qty = per_slot * LEVERAGE
                cash += old_margin - new_margin
                pos_r["margin"] = new_margin
                pos_r["qty_usdt"] = new_qty

            order_usdt = per_slot * LEVERAGE
            margin = per_slot

            for sym, sk, score, cur_price in selected:
                if len(positions) >= MAX_POS:
                    break
                if cash < margin:
                    break

                cfg = STRATS[sk]
                direction = cfg["direction"]

                cash -= margin

                positions[sym] = {
                    "strat": sk,
                    "direction": direction,
                    "entry_price": cur_price,
                    "entry_date": today,
                    "qty_usdt": order_usdt,
                    "margin": margin,
                    "side": "Buy" if direction == "long" else "Sell",
                }

        # 청산 후 남은 포지션 리사이징 (확대)
        if n_before > len(positions) and len(positions) > 0 and not selected:
            invest_capital = get_equity_now(today) * (1 - effective_cash_ratio)
            per_slot = invest_capital / len(positions)
            for sym_r, pos_r in positions.items():
                old_margin = pos_r["margin"]
                new_margin = per_slot
                new_qty = per_slot * LEVERAGE
                cash += old_margin - new_margin
                pos_r["margin"] = new_margin
                pos_r["qty_usdt"] = new_qty

        # ── 자산 평가 (라이브 동일: 현금 + 포지션 평가) ──
        equity = get_equity_now(today)
        equity_curve.append({"date": today, "equity": equity})

        # 진행상황 (월 1회)
        if day_idx > 0 and today[8:10] == "01":
            log.info(f"[{today}] 자산=${equity:,.0f} 포지션={len(positions)} 거래={len(trade_log)}건")

    # ── 결과 출력 ──
    print_results(trade_log, equity_curve)

    return trade_log, equity_curve


def _close_position(positions, pos, symbol, pnl, date, reason, trade_log):
    """포지션 청산 기록"""
    n_pos = len(positions)
    trade_log.append({
        "symbol": symbol,
        "strat": pos["strat"],
        "direction": pos["direction"],
        "entry_price": pos["entry_price"],
        "entry_date": pos["entry_date"],
        "exit_date": date,
        "pnl": pnl,  # 비율 (0.05 = 5%)
        "reason": reason,
        "n_pos": n_pos,
    })
    del positions[symbol]


def print_results(trade_log: list, equity_curve: list):
    """백테스트 결과 출력"""
    # stdout 인코딩 설정
    import io
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    print("\n" + "=" * 70)
    print("  백테스트 결과 - 바이비트 채널 돌파 복합 전략 v3 [동적 1/n]")
    print("=" * 70)

    if not equity_curve:
        print("데이터 없음")
        return

    eq_df = pd.DataFrame(equity_curve)
    final_equity = eq_df["equity"].iloc[-1]
    total_ret = (final_equity / INITIAL_CAPITAL - 1) * 100

    # CAGR
    start_dt = pd.Timestamp(eq_df["date"].iloc[0])
    end_dt = pd.Timestamp(eq_df["date"].iloc[-1])
    years = (end_dt - start_dt).days / 365.0
    if years > 0 and final_equity > 0:
        cagr = ((final_equity / INITIAL_CAPITAL) ** (1 / years) - 1) * 100
    else:
        cagr = 0

    # MDD
    eq_df["peak"] = eq_df["equity"].cummax()
    eq_df["dd"] = eq_df["equity"] / eq_df["peak"] - 1
    mdd = eq_df["dd"].min() * 100

    # 거래 통계
    n_trades = len(trade_log)
    if n_trades > 0:
        wins = sum(1 for t in trade_log if t["pnl"] >= 0)
        win_rate = wins / n_trades * 100
        avg_pnl = np.mean([t["pnl"] for t in trade_log]) * 100
        avg_win = np.mean([t["pnl"] for t in trade_log if t["pnl"] >= 0]) * 100 if wins > 0 else 0
        avg_loss = np.mean([t["pnl"] for t in trade_log if t["pnl"] < 0]) * 100 if (n_trades - wins) > 0 else 0

        # 전략별 통계
        strat_stats = {}
        for sk in STRATS:
            st = [t for t in trade_log if t["strat"] == sk]
            if st:
                sw = sum(1 for t in st if t["pnl"] >= 0)
                strat_stats[sk] = {
                    "count": len(st),
                    "win_rate": sw / len(st) * 100,
                    "avg_pnl": np.mean([t["pnl"] for t in st]) * 100,
                }

        # 청산 사유별
        reason_stats = {}
        for t in trade_log:
            r = t["reason"].split(" ")[0]
            if r not in reason_stats:
                reason_stats[r] = {"count": 0, "pnl_sum": 0}
            reason_stats[r]["count"] += 1
            reason_stats[r]["pnl_sum"] += t["pnl"]
    else:
        win_rate = avg_pnl = avg_win = avg_loss = 0
        strat_stats = {}
        reason_stats = {}

    print(f"\n  기간: {eq_df['date'].iloc[0]} ~ {eq_df['date'].iloc[-1]} ({len(eq_df)}일)")
    print(f"  시작자본: ${INITIAL_CAPITAL:,.0f}")
    print(f"  최종자산: ${final_equity:,.0f}")
    print(f"  총수익률: {total_ret:+.1f}%")
    print(f"  CAGR:     {cagr:+.1f}%")
    print(f"  MDD:      {mdd:.1f}%")
    print(f"  레버리지:  {LEVERAGE}x | 현금비율: {CASH_RATIO*100:.0f}%")
    print(f"\n  총 거래: {n_trades}건")
    print(f"  승률:   {win_rate:.1f}%")
    print(f"  평균수익: {avg_pnl:+.2f}%")
    print(f"  평균이익: {avg_win:+.2f}% | 평균손실: {avg_loss:+.2f}%")

    if strat_stats:
        print(f"\n  ── 전략별 ──")
        for sk, ss in strat_stats.items():
            cfg = STRATS[sk]
            print(f"  {sk}({cfg['name']}): {ss['count']}건 승률{ss['win_rate']:.0f}% 평균{ss['avg_pnl']:+.2f}%")

    if reason_stats:
        print(f"\n  ── 청산 사유별 ──")
        for r, rs in sorted(reason_stats.items()):
            avg = rs["pnl_sum"] / rs["count"] * 100
            print(f"  {r}: {rs['count']}건 평균{avg:+.2f}%")

    # 월별 수익률
    eq_df["month"] = eq_df["date"].str[:7]
    monthly = eq_df.groupby("month")["equity"].last()
    monthly_ret = monthly.pct_change() * 100

    print(f"\n  ── 월별 수익률 ──")
    for month, ret in monthly_ret.items():
        if pd.notna(ret):
            bar = "█" * max(0, int(ret / 2)) if ret >= 0 else "▓" * max(0, int(-ret / 2))
            print(f"  {month}: {ret:+6.1f}% {bar}")

    print("\n" + "=" * 70)

    # 결과 저장
    result_file = os.path.join(CACHE_DIR, "bt_result.pkl")
    with open(result_file, "wb") as f:
        pickle.dump({"trades": trade_log, "equity": equity_curve}, f)
    print(f"\n결과 저장: {result_file}")


# ── 메인 ───────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("  바이비트 채널 돌파 백테스트 시작")
    log.info(f"  기간: {BT_START} ~ {BT_END}")
    log.info(f"  레버리지={LEVERAGE}x, 슬롯={MAX_POS}, 현금={CASH_RATIO*100:.0f}%")
    log.info(f"  유니버스: 전년 거래대금 상위 {TOP_N}")
    log.info("=" * 60)

    # 1. 유니버스 종목 조회
    log.info("종목 목록 조회 중...")
    all_symbols = get_universe_symbols()
    log.info(f"전체 후보: {len(all_symbols)}종목")

    # 2. 데이터 다운로드
    log.info("데이터 다운로드 중...")
    data = download_all_data(all_symbols, BT_START, BT_END)
    log.info(f"데이터 완료: {len(data)}종목")

    # 3. 백테스트 실행
    log.info("백테스트 실행 중...")
    run_backtest(data, all_symbols)


if __name__ == "__main__":
    main()
