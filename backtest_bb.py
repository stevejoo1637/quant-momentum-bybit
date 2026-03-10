#!/usr/bin/env python3
"""
볼린저밴드 하단 터치 롱 전략 백테스트
- main.py와 동일한 로직을 과거 데이터로 검증
- BB(20,2) 하단 터치 + RSI < 35 → 롱 진입
- ATR 기반 TP(2x) / SL(1x)
"""

import os
import json
import numpy as np
import pandas as pd
from datetime import datetime

# ── 설정 ─────────────────────────────────────────────────────────────────────
LEVERAGE = 3
MAX_POS = 4
INITIAL_CAPITAL = 10000

BB_PERIOD = 20
BB_STD = 2.0
RSI_PERIOD = 14
RSI_THRESHOLD = 35
ATR_PERIOD = 14
TP_ATR_MULT = 2.0
SL_ATR_MULT = 1.0

TOP_N = 60
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


# ── 지표 계산 ────────────────────────────────────────────────────────────────
def calc_bb(close):
    sma = close.rolling(BB_PERIOD).mean()
    std = close.rolling(BB_PERIOD).std()
    upper = sma + BB_STD * std
    lower = sma - BB_STD * std
    return upper, sma, lower


def calc_rsi(close):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def calc_atr(high, low, close):
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(ATR_PERIOD).mean()


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


def prepare_data(df):
    """지표 추가"""
    df = df.copy()
    df["bb_upper"], df["bb_mid"], df["bb_lower"] = calc_bb(df["close"])
    df["rsi"] = calc_rsi(df["close"])
    df["atr"] = calc_atr(df["high"], df["low"], df["close"])
    return df


# ── 백테스트 엔진 ────────────────────────────────────────────────────────────
def run_backtest():
    print("=" * 60)
    print("  BB 하단 터치 롱 전략 백테스트")
    print("=" * 60)

    universe = load_universe()
    print(f"유니버스: {len(universe)}종목")

    # 데이터 로드 & 지표 계산
    all_data = {}
    for sym in universe:
        df = load_csv(sym)
        if df is not None and len(df) >= BB_PERIOD + 5:
            all_data[sym] = prepare_data(df)
    print(f"데이터 로드: {len(all_data)}종목")

    # 거래 날짜
    all_dates = set()
    for df in all_data.values():
        for d in df["date"]:
            all_dates.add(d.strftime("%Y-%m-%d"))
    all_dates = sorted(all_dates)

    if len(all_dates) < 30:
        print("ERROR: 충분한 데이터 없음")
        return

    print(f"기간: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)}일)")
    print(f"설정: 레버리지={LEVERAGE}x, 슬롯={MAX_POS}")
    print(f"BB({BB_PERIOD},{BB_STD}) | RSI<{RSI_THRESHOLD} | TP={TP_ATR_MULT}ATR SL={SL_ATR_MULT}ATR")
    print()

    # 상태
    equity = INITIAL_CAPITAL
    positions = {}  # {symbol: {entry_price, tp, sl, entry_date}}
    trade_log = []
    equity_curve = []

    for date_str in all_dates:
        # ── 1. 청산 체크 ──
        for sym in list(positions.keys()):
            pos = positions[sym]
            sym_df = all_data.get(sym)
            if sym_df is None:
                continue
            row = sym_df[sym_df["date"].dt.strftime("%Y-%m-%d") == date_str]
            if row.empty:
                continue

            high = row.iloc[0]["high"]
            low = row.iloc[0]["low"]
            close = row.iloc[0]["close"]

            # SL 체크 (저가 기준)
            if low <= pos["sl"]:
                pnl_pct = (pos["sl"] / pos["entry_price"] - 1)
                realized = pos["size"] * pnl_pct * LEVERAGE
                equity += realized
                trade_log.append({
                    "symbol": sym, "entry_date": pos["entry_date"],
                    "exit_date": date_str, "entry_price": pos["entry_price"],
                    "exit_price": pos["sl"], "pnl_pct": pnl_pct * 100,
                    "pnl_usdt": realized, "reason": "SL",
                })
                del positions[sym]
                continue

            # TP 체크 (고가 기준)
            if high >= pos["tp"]:
                pnl_pct = (pos["tp"] / pos["entry_price"] - 1)
                realized = pos["size"] * pnl_pct * LEVERAGE
                equity += realized
                trade_log.append({
                    "symbol": sym, "entry_date": pos["entry_date"],
                    "exit_date": date_str, "entry_price": pos["entry_price"],
                    "exit_price": pos["tp"], "pnl_pct": pnl_pct * 100,
                    "pnl_usdt": realized, "reason": "TP",
                })
                del positions[sym]
                continue

        # ── 2. 진입 체크 ──
        if len(positions) < MAX_POS:
            per_slot = equity / MAX_POS
            candidates = []

            for sym in all_data:
                if sym in positions:
                    continue

                sym_df = all_data[sym]
                rows = sym_df[sym_df["date"].dt.strftime("%Y-%m-%d") == date_str]
                if rows.empty:
                    continue
                idx = rows.index[0]
                if idx < BB_PERIOD + 2:
                    continue

                curr = sym_df.loc[idx]
                prev = sym_df.loc[idx - 1]

                if pd.isna(curr["bb_lower"]) or pd.isna(curr["rsi"]) or pd.isna(curr["atr"]):
                    continue

                # BB 하단 터치 + RSI 과매도
                touch = prev["close"] > prev["bb_lower"] and curr["close"] <= curr["bb_lower"]
                oversold = curr["rsi"] < RSI_THRESHOLD

                if touch and oversold:
                    score = (RSI_THRESHOLD - curr["rsi"]) * curr["atr"]
                    candidates.append((sym, idx, score))

            # 점수순 진입
            candidates.sort(key=lambda x: -x[2])
            for sym, idx, score in candidates:
                if len(positions) >= MAX_POS:
                    break

                curr = all_data[sym].loc[idx]
                price = curr["close"]
                atr = curr["atr"]
                tp = price + atr * TP_ATR_MULT
                sl = price - atr * SL_ATR_MULT

                positions[sym] = {
                    "entry_price": price,
                    "tp": tp,
                    "sl": sl,
                    "entry_date": date_str,
                    "size": per_slot,
                }

        # ── 3. 일일 평가 ──
        unrealized = 0
        for sym, pos in positions.items():
            sym_df = all_data.get(sym)
            if sym_df is None:
                continue
            row = sym_df[sym_df["date"].dt.strftime("%Y-%m-%d") == date_str]
            if row.empty:
                continue
            cur_price = row.iloc[0]["close"]
            pnl_pct = cur_price / pos["entry_price"] - 1
            unrealized += pos["size"] * pnl_pct * LEVERAGE

        equity_curve.append({
            "date": date_str,
            "equity": equity + unrealized,
            "positions": len(positions),
        })

    # ── 결과 출력 ────────────────────────────────────────────────────────────
    print_results(trade_log, equity_curve, equity)


def print_results(trade_log, equity_curve, final_equity):
    if not equity_curve:
        print("거래 없음")
        return

    eq_df = pd.DataFrame(equity_curve)
    eq_df["date"] = pd.to_datetime(eq_df["date"])

    total_realized = sum(t["pnl_usdt"] for t in trade_log)
    final_equity = INITIAL_CAPITAL + total_realized

    peak = eq_df["equity"].cummax()
    drawdown = (eq_df["equity"] / peak - 1) * 100
    max_dd = drawdown.min()

    days = (eq_df["date"].iloc[-1] - eq_df["date"].iloc[0]).days
    if days > 0 and final_equity > 0:
        cagr = (final_equity / INITIAL_CAPITAL) ** (365.0 / days) - 1
    else:
        cagr = 0

    total_return = (final_equity / INITIAL_CAPITAL - 1) * 100
    n_trades = len(trade_log)

    if n_trades > 0:
        wins = sum(1 for t in trade_log if t["pnl_pct"] >= 0)
        win_rate = wins / n_trades * 100
        avg_pnl = np.mean([t["pnl_pct"] for t in trade_log])
        avg_win = np.mean([t["pnl_pct"] for t in trade_log if t["pnl_pct"] >= 0]) if wins > 0 else 0
        avg_loss = np.mean([t["pnl_pct"] for t in trade_log if t["pnl_pct"] < 0]) if wins < n_trades else 0
    else:
        wins = win_rate = avg_pnl = avg_win = avg_loss = 0

    # 청산 사유별
    reason_stats = {}
    for t in trade_log:
        r = t["reason"]
        if r not in reason_stats:
            reason_stats[r] = {"count": 0, "pnl": 0}
        reason_stats[r]["count"] += 1
        reason_stats[r]["pnl"] += t["pnl_usdt"]

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

    if reason_stats:
        print(f"\n  청산 사유:")
        for r, st in reason_stats.items():
            print(f"    {r}: {st['count']}회 | ${st['pnl']:+,.0f}")

    # 월별 수익률
    monthly = {}
    for t in trade_log:
        month = t["exit_date"][:7]
        monthly[month] = monthly.get(month, 0) + t["pnl_usdt"]

    if monthly:
        print(f"\n  월별 손익:")
        for m in sorted(monthly.keys()):
            bar = "+" * max(0, int(monthly[m] / 50)) if monthly[m] > 0 else "-" * max(0, int(-monthly[m] / 50))
            print(f"    {m}: ${monthly[m]:>+10,.0f} {bar}")

    # 최근 거래
    if trade_log:
        print(f"\n  최근 거래 (최대 10건):")
        print(f"  {'날짜':<12} {'종목':<14} {'수익률':>8} {'사유':<4}")
        print("  " + "-" * 42)
        for t in trade_log[-10:]:
            print(f"  {t['exit_date']:<12} {t['symbol']:<14} {t['pnl_pct']:>+7.2f}% {t['reason']:<4}")

    # CSV 저장
    if trade_log:
        trades_df = pd.DataFrame(trade_log)
        trades_df.to_csv(os.path.join(DATA_DIR, "bb_backtest_trades.csv"), index=False)
        eq_df.to_csv(os.path.join(DATA_DIR, "bb_backtest_equity.csv"), index=False)
        print(f"\n  거래내역: data/bb_backtest_trades.csv")
        print(f"  자산곡선: data/bb_backtest_equity.csv")


if __name__ == "__main__":
    run_backtest()
