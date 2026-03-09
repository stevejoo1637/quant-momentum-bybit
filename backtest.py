#!/usr/bin/env python3
"""
바이비트 채널 돌파 복합 전략 백테스트

VPS 실행 전략과 동일한 로직:
  A: 상단돌파 롱 (강세, SL-7%/TP+25%/7일, R²>0.5, 볼륨1.5x)
  B: 하단돌파 롱 (무관, SL-5%/TP+15%/14일, R²>0.5, 볼륨1.0x)
  C: 상단터치 숏 (약세, SL-15%/TP+20%/10일, R²>0.3, 볼륨1.0x)

설정: 레버리지 2x, 슬롯 4, 현금비율 40%, BTC SMA필터
"""

import os
import json
import numpy as np
import pandas as pd
from datetime import datetime

# ── 설정 ─────────────────────────────────────────────────────────────────────

LEVERAGE = 2
MAX_POS = 4
CASH_RATIO = 0.40
MDD_DEPLOY_THRESH = -0.35
TOP_N = 60
INITIAL_CAPITAL = 10000

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
CHANNEL_STD = 2.0

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


# ── 채널 계산 ────────────────────────────────────────────────────────────────

def calc_channel(closes):
    """선형 회귀 채널 (VPS 코드와 동일)"""
    n = CHANNEL_PERIOD
    if len(closes) < n:
        return None

    y = np.array(closes[-n:])
    x = np.arange(n)

    # 선형 회귀
    x_mean = x.mean()
    y_mean = y.mean()
    ss_xx = ((x - x_mean) ** 2).sum()
    ss_xy = ((x - x_mean) * (y - y_mean)).sum()

    if ss_xx == 0:
        return None

    slope = ss_xy / ss_xx
    intercept = y_mean - slope * x_mean

    y_pred = slope * x + intercept
    ss_res = ((y - y_pred) ** 2).sum()
    ss_tot = ((y - y_mean) ** 2).sum()

    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

    std = np.std(y - y_pred)
    mid = y_pred[-1]
    upper = mid + CHANNEL_STD * std
    lower = mid - CHANNEL_STD * std

    return {"r2": r2, "upper": upper, "lower": lower, "mid": mid, "slope": slope}


# ── 데이터 로드 ──────────────────────────────────────────────────────────────

def load_csv(symbol):
    path = os.path.join(DATA_DIR, f"{symbol}.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def load_universe():
    path = os.path.join(DATA_DIR, "universe.json")
    with open(path) as f:
        items = json.load(f)
    return [item["symbol"] for item in items[:TOP_N]]


# ── BTC 시장 상태 ────────────────────────────────────────────────────────────

def calc_btc_sma(btc_df):
    """BTC SMA20/SMA50 계산 → 날짜별 강세/약세 딕셔너리"""
    btc_df = btc_df.copy()
    btc_df["sma20"] = btc_df["close"].rolling(20).mean()
    btc_df["sma50"] = btc_df["close"].rolling(50).mean()
    btc_df["is_bull"] = btc_df["sma20"] > btc_df["sma50"]

    result = {}
    for _, row in btc_df.iterrows():
        if pd.notna(row["sma20"]) and pd.notna(row["sma50"]):
            result[row["date"].strftime("%Y-%m-%d")] = row["is_bull"]
    return result


# ── 백테스트 엔진 ────────────────────────────────────────────────────────────

def run_backtest():
    print("=" * 60)
    print("  채널 돌파 복합 전략 백테스트")
    print("=" * 60)

    # BTC 데이터
    btc_df = load_csv("BTCUSDT")
    if btc_df is None:
        print("ERROR: BTCUSDT.csv 없음. fetch_data.py 먼저 실행하세요.")
        return
    btc_market = calc_btc_sma(btc_df)

    # 유니버스
    universe = load_universe()
    print(f"유니버스: {len(universe)}종목")

    # 전체 종목 데이터 로드
    all_data = {}
    for sym in universe:
        df = load_csv(sym)
        if df is not None and len(df) >= CHANNEL_PERIOD + 2:
            all_data[sym] = df
    print(f"데이터 로드: {len(all_data)}종목")

    # 거래 날짜 범위
    all_dates = set()
    for df in all_data.values():
        for d in df["date"]:
            all_dates.add(d.strftime("%Y-%m-%d"))
    all_dates = sorted(all_dates)

    # BTC 데이터가 있는 날짜만
    trade_dates = [d for d in all_dates if d in btc_market]
    if len(trade_dates) < 60:
        print("ERROR: 충분한 데이터 없음")
        return

    print(f"백테스트 기간: {trade_dates[0]} ~ {trade_dates[-1]} ({len(trade_dates)}일)")
    print(f"설정: 레버리지={LEVERAGE}x, 슬롯={MAX_POS}, 현금비율={CASH_RATIO*100:.0f}%")
    for sk, cfg in STRATS.items():
        print(f"  {sk}: {cfg['name']} SL={cfg['sl']*100:.0f}% TP={cfg['tp']*100:.0f}% {cfg['hold_days']}일")
    print()

    # 상태 초기화
    equity = INITIAL_CAPITAL
    peak_equity = equity
    positions = {}  # {symbol: {strat, direction, entry_price, entry_date, qty, sl, tp, hold_days}}
    trade_log = []
    equity_curve = []
    mdd_deployed = False

    for date_str in trade_dates:
        is_bull = btc_market[date_str]

        # ── 1. BTC 필터 청산 ──
        for sym in list(positions.keys()):
            pos = positions[sym]
            sk = pos["strat"]
            btcf = STRATS[sk]["btc_filter"]
            if (btcf == "bull" and not is_bull) or (btcf == "bear" and is_bull):
                # 현재가 조회
                sym_df = all_data.get(sym)
                if sym_df is None:
                    continue
                row = sym_df[sym_df["date"].dt.strftime("%Y-%m-%d") == date_str]
                if row.empty:
                    continue
                cur_price = row.iloc[0]["close"]
                pnl = _calc_pnl(pos, cur_price)
                _close_position(positions, trade_log, sym, pos, cur_price, pnl, date_str, "BTC필터")

        # ── 2. SL/TP/TIME 청산 ──
        for sym in list(positions.keys()):
            pos = positions[sym]
            sym_df = all_data.get(sym)
            if sym_df is None:
                continue
            row = sym_df[sym_df["date"].dt.strftime("%Y-%m-%d") == date_str]
            if row.empty:
                continue

            cur_price = row.iloc[0]["close"]
            pnl = _calc_pnl(pos, cur_price)
            held = (datetime.strptime(date_str, "%Y-%m-%d") - datetime.strptime(pos["entry_date"], "%Y-%m-%d")).days

            cfg = STRATS[pos["strat"]]
            if pnl <= -cfg["sl"]:
                _close_position(positions, trade_log, sym, pos, cur_price, pnl, date_str, "SL")
            elif pnl >= cfg["tp"]:
                _close_position(positions, trade_log, sym, pos, cur_price, pnl, date_str, "TP")
            elif held >= cfg["hold_days"]:
                _close_position(positions, trade_log, sym, pos, cur_price, pnl, date_str, "TIME")

        # ── 3. 신규 진입 ──
        avail_slots = MAX_POS - len(positions)
        if avail_slots > 0:
            # MDD 기반 현금 비율
            if equity > peak_equity:
                peak_equity = equity
                mdd_deployed = False
            current_mdd = equity / peak_equity - 1 if peak_equity > 0 else 0

            if current_mdd <= MDD_DEPLOY_THRESH and not mdd_deployed:
                effective_cash_ratio = 0.0
                mdd_deployed = True
            elif mdd_deployed:
                effective_cash_ratio = 0.0
            else:
                effective_cash_ratio = CASH_RATIO

            invest_capital = equity * (1 - effective_cash_ratio)
            per_slot = invest_capital / MAX_POS

            # 후보 스캔
            candidates = []
            held_symbols = set(positions.keys())

            for sym in list(all_data.keys()):
                if sym in held_symbols:
                    continue

                sym_df = all_data[sym]
                idx_rows = sym_df[sym_df["date"].dt.strftime("%Y-%m-%d") == date_str]
                if idx_rows.empty:
                    continue
                idx = idx_rows.index[0]
                if idx < CHANNEL_PERIOD + 1:
                    continue

                closes = sym_df.loc[:idx, "close"].tolist()
                volumes = sym_df.loc[:idx, "volume"].tolist()

                channel = calc_channel(closes)
                if channel is None:
                    continue

                r2 = channel["r2"]
                upper = channel["upper"]
                lower = channel["lower"]

                prev_close = closes[-2]
                curr_close = closes[-1]

                vol_window = volumes[-CHANNEL_PERIOD:]
                vol_ma = np.mean(vol_window)
                if vol_ma <= 0:
                    continue
                vol_ratio = volumes[-1] / vol_ma

                # 5일 모멘텀
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
                        candidates.append((sym, sk, score, curr_close))

            # 점수순 정렬 & 진입
            candidates.sort(key=lambda x: -x[2])
            entered = set(held_symbols)

            for sym, sk, score, cur_price in candidates:
                if len(positions) >= MAX_POS:
                    break
                if sym in entered:
                    continue

                cfg = STRATS[sk]
                direction = cfg["direction"]

                order_usdt = per_slot * LEVERAGE
                qty = order_usdt / cur_price

                positions[sym] = {
                    "strat": sk,
                    "direction": direction,
                    "entry_price": cur_price,
                    "entry_date": date_str,
                    "qty": qty,
                }
                entered.add(sym)

        # ── 4. 일일 평가 ──
        day_equity = equity
        # 미체결 포지션 평가손익 (레버리지 적용)
        unrealized = 0
        for sym, pos in positions.items():
            sym_df = all_data.get(sym)
            if sym_df is None:
                continue
            row = sym_df[sym_df["date"].dt.strftime("%Y-%m-%d") == date_str]
            if row.empty:
                continue
            cur_price = row.iloc[0]["close"]
            pnl_pct = _calc_pnl(pos, cur_price)
            pos_value = pos["qty"] * pos["entry_price"] / LEVERAGE  # 실제 투입 증거금
            unrealized += pos_value * pnl_pct * LEVERAGE

        equity_curve.append({
            "date": date_str,
            "equity": day_equity + unrealized,
            "cash_equity": equity,
            "positions": len(positions),
            "is_bull": is_bull,
        })

    # ── 결과 출력 ────────────────────────────────────────────────────────────
    print_results(trade_log, equity_curve, equity)


def _calc_pnl(pos, cur_price):
    """수익률 계산 (레버리지 미적용 순수 가격 변동률)"""
    entry = pos["entry_price"]
    if pos["direction"] == "short":
        return -(cur_price / entry - 1)
    else:
        return cur_price / entry - 1


def _close_position(positions, trade_log, sym, pos, cur_price, pnl, date_str, reason):
    """포지션 청산 & 로그 기록"""
    held = (datetime.strptime(date_str, "%Y-%m-%d") - datetime.strptime(pos["entry_date"], "%Y-%m-%d")).days
    pos_value = pos["qty"] * pos["entry_price"] / LEVERAGE
    realized_pnl = pos_value * pnl * LEVERAGE  # 실제 손익 (레버리지 적용)

    trade_log.append({
        "symbol": sym,
        "strat": pos["strat"],
        "direction": pos["direction"],
        "entry_price": pos["entry_price"],
        "exit_price": cur_price,
        "pnl_pct": pnl * 100,
        "pnl_usdt": realized_pnl,
        "reason": reason,
        "held": held,
        "entry_date": pos["entry_date"],
        "exit_date": date_str,
    })

    # equity 업데이트는 호출자의 equity를 직접 수정할 수 없으므로
    # equity_curve에서 cash_equity로 추적
    # → 간단하게: positions dict에서 제거하면 unrealized가 0이 되므로
    #   realized를 별도 누적해야 함 → global 대신 positions에 기록
    pos["closed_pnl"] = realized_pnl
    del positions[sym]


def print_results(trade_log, equity_curve, final_cash_equity):
    """백테스트 결과 출력"""
    if not equity_curve:
        print("거래 없음")
        return

    # equity curve에서 실제 자산 추이
    eq_df = pd.DataFrame(equity_curve)
    eq_df["date"] = pd.to_datetime(eq_df["date"])

    # 실현 손익 누적으로 최종 자산 계산
    total_realized = sum(t["pnl_usdt"] for t in trade_log)
    final_equity = INITIAL_CAPITAL + total_realized

    # equity curve 기반 통계
    peak = eq_df["equity"].cummax()
    drawdown = (eq_df["equity"] / peak - 1) * 100
    max_dd = drawdown.min()

    # CAGR
    days = (eq_df["date"].iloc[-1] - eq_df["date"].iloc[0]).days
    if days > 0 and final_equity > 0:
        years = days / 365.0
        cagr = (final_equity / INITIAL_CAPITAL) ** (1.0 / years) - 1
    else:
        cagr = 0

    total_return = (final_equity / INITIAL_CAPITAL - 1) * 100

    # 거래 통계
    n_trades = len(trade_log)
    if n_trades > 0:
        wins = sum(1 for t in trade_log if t["pnl_pct"] >= 0)
        win_rate = wins / n_trades * 100
        avg_pnl = np.mean([t["pnl_pct"] for t in trade_log])
        avg_win = np.mean([t["pnl_pct"] for t in trade_log if t["pnl_pct"] >= 0]) if wins > 0 else 0
        avg_loss = np.mean([t["pnl_pct"] for t in trade_log if t["pnl_pct"] < 0]) if wins < n_trades else 0
        avg_held = np.mean([t["held"] for t in trade_log])
    else:
        wins = win_rate = avg_pnl = avg_win = avg_loss = avg_held = 0

    # 전략별 통계
    strat_stats = {}
    for sk in STRATS:
        st_trades = [t for t in trade_log if t["strat"] == sk]
        if st_trades:
            st_wins = sum(1 for t in st_trades if t["pnl_pct"] >= 0)
            strat_stats[sk] = {
                "trades": len(st_trades),
                "win_rate": st_wins / len(st_trades) * 100,
                "avg_pnl": np.mean([t["pnl_pct"] for t in st_trades]),
                "total_pnl": sum(t["pnl_usdt"] for t in st_trades),
            }

    # 월별 수익률
    monthly = {}
    for t in trade_log:
        month = t["exit_date"][:7]
        monthly[month] = monthly.get(month, 0) + t["pnl_usdt"]

    # 출력
    print("\n" + "=" * 60)
    print("  백테스트 결과")
    print("=" * 60)
    print(f"  기간: {eq_df['date'].iloc[0].strftime('%Y-%m-%d')} ~ {eq_df['date'].iloc[-1].strftime('%Y-%m-%d')} ({days}일)")
    print(f"  초기자산: ${INITIAL_CAPITAL:,.0f}")
    print(f"  최종자산: ${final_equity:,.0f}")
    print(f"  총수익률: {total_return:+.1f}%")
    print(f"  CAGR:     {cagr*100:+.1f}%")
    print(f"  MDD:      {max_dd:.1f}%")
    print(f"  거래횟수: {n_trades}회")
    print(f"  승률:     {win_rate:.1f}%")
    print(f"  평균수익: {avg_pnl:+.2f}%")
    print(f"  평균승:   {avg_win:+.2f}% | 평균패: {avg_loss:+.2f}%")
    print(f"  평균보유: {avg_held:.1f}일")

    if strat_stats:
        print(f"\n{'전략':<6} {'거래':>5} {'승률':>7} {'평균':>8} {'총손익':>12}")
        print("-" * 42)
        for sk, st in strat_stats.items():
            print(f"  {sk:<4} {st['trades']:>5} {st['win_rate']:>6.1f}% {st['avg_pnl']:>+7.2f}% ${st['total_pnl']:>+10,.0f}")

    if monthly:
        print(f"\n월별 손익:")
        for m in sorted(monthly.keys()):
            bar = "+" * max(0, int(monthly[m] / 50)) if monthly[m] > 0 else "-" * max(0, int(-monthly[m] / 50))
            print(f"  {m}: ${monthly[m]:>+10,.0f} {bar}")

    # 최근 거래 10건
    if trade_log:
        print(f"\n최근 거래 (최대 10건):")
        print(f"{'날짜':<12} {'종목':<14} {'전략':<4} {'방향':<5} {'수익률':>8} {'사유':<8}")
        print("-" * 55)
        for t in trade_log[-10:]:
            dir_tag = "숏" if t["direction"] == "short" else "롱"
            print(f"  {t['exit_date']:<10} {t['symbol']:<14} {t['strat']:<4} {dir_tag:<4} {t['pnl_pct']:>+7.2f}% {t['reason']:<8}")

    # CSV 저장
    if trade_log:
        trades_df = pd.DataFrame(trade_log)
        trades_df.to_csv(os.path.join(DATA_DIR, "backtest_trades.csv"), index=False)
        eq_df.to_csv(os.path.join(DATA_DIR, "backtest_equity.csv"), index=False)
        print(f"\n거래내역 저장: data/backtest_trades.csv")
        print(f"자산곡선 저장: data/backtest_equity.csv")


if __name__ == "__main__":
    run_backtest()
