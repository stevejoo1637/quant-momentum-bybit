#!/usr/bin/env python3
"""
채널 돌파 복합 전략 - 최적화 백테스트 (3배, 동적1/n)
======================================================
[최적화 파라미터 적용]
A: 상단돌파 롱 (강세, SL-7%/TP+40%/7일, R²>0.5, 볼륨1.5x)
B: 하단돌파 롱 (무관, SL-10%/TP+15%/14일, R²>0.5, 볼륨1.0x)
C: 상단터치 숏 (약세, SL-10%/TP+15%/10일, R²>0.3, 볼륨1.0x)
포지션: 동적 1/n (보유 수에 따라 균등배분), 3슬롯
시장필터: BTC SMA20 > SMA50
레버리지: 3배
현금: 40% + MDD-35%→전량투입
수수료: 편도 0.1%
유니버스: 매년 전년 거래대금 상위 60개 (BTC/ETH 제외)
데이터: bt_cache.pkl (Bybit API 다운로드 캐시)
"""
import os
import sys
import numpy as np
import pandas as pd
import pickle
import time

# ─── 설정 ────────────────────────────────────────────────────
COST_PER_SIDE = 0.001
START_DATE = "2023-01-01"
TOP_N = 60
EXCLUDE = {"BTCUSDT", "ETHUSDT"}
MAX_POS = 3
LEVERAGE = 3
CASH_RATIO = 0.40
MDD_DEPLOY_THRESH = -0.35
INITIAL_CAPITAL = 10000.0

STRATS = {
    "A": {
        "name": "상단돌파 롱",
        "signal": "upper_break",
        "direction": "long",
        "btc_filter": "bull",
        "sl": -0.07, "tp": 0.40, "hold_days": 7,
        "r2_thresh": 0.5, "vol_mult": 1.5,
    },
    "B": {
        "name": "하단돌파 롱",
        "signal": "lower_break",
        "direction": "long",
        "btc_filter": "none",
        "sl": -0.10, "tp": 0.15, "hold_days": 14,
        "r2_thresh": 0.5, "vol_mult": 1.0,
    },
    "C": {
        "name": "상단터치 숏",
        "signal": "upper_touch",
        "direction": "short",
        "btc_filter": "bear",
        "sl": -0.10, "tp": 0.15, "hold_days": 10,
        "r2_thresh": 0.3, "vol_mult": 1.0,
    },
}

CACHE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(CACHE_DIR, "bt_cache.pkl")

# ─── 채널 계산 ───────────────────────────────────────────────
def calc_linear_regression_channel(prices, period=20, std_mult=2.0):
    n = len(prices)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    r2 = np.full(n, np.nan)
    x = np.arange(period)
    x_mean = x.mean()
    x_var = ((x - x_mean) ** 2).sum()
    for i in range(period - 1, n):
        y = prices[i - period + 1 : i + 1]
        if np.isnan(y).any():
            continue
        y_mean = y.mean()
        slope = ((x - x_mean) * (y - y_mean)).sum() / x_var
        intercept = y_mean - slope * x_mean
        trend_vals = slope * x + intercept
        resid = y - trend_vals
        std_r = resid.std()
        ss_res = (resid ** 2).sum()
        ss_tot = ((y - y_mean) ** 2).sum()
        upper[i] = trend_vals[-1] + std_mult * std_r
        lower[i] = trend_vals[-1] - std_mult * std_r
        r2[i] = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    return upper, lower, r2

# ─── 데이터 로드 (bt_cache.pkl → close_all, volume_all) ─────
def load_cache_data():
    """bt_cache.pkl을 close/volume DataFrame으로 변환"""
    with open(CACHE_FILE, "rb") as f:
        cache = pickle.load(f)
    print(f"  캐시 로드: {len(cache)}종목")

    close_dict = {}
    volume_dict = {}
    for sym, df in cache.items():
        if df.empty:
            continue
        s = df.set_index("date").sort_index()
        s.index = pd.to_datetime(s.index)
        close_dict[sym] = s["close"]
        volume_dict[sym] = s["volume"]

    close_all = pd.DataFrame(close_dict).sort_index()
    volume_all = pd.DataFrame(volume_dict).sort_index()
    return close_all, volume_all

# ─── 유니버스 ────────────────────────────────────────────────
def build_annual_universe(close_all, volume_all):
    turnover = close_all * volume_all
    start_year = int(START_DATE[:4])
    end_year = close_all.index[-1].year
    universe = {}
    universe_rank = {}
    for y in range(start_year, end_year + 1):
        prev_year = str(y - 1)
        if prev_year in turnover.index.year.astype(str).values:
            tv_prev = turnover.loc[prev_year]
        else:
            tv_prev = turnover.loc[: f"{y-1}-12-31"]
        if len(tv_prev) == 0:
            universe[y] = []
            universe_rank[y] = {}
            continue
        avg_tv = tv_prev.mean().dropna().sort_values(ascending=False)
        avg_tv = avg_tv.drop(
            labels=[s for s in EXCLUDE if s in avg_tv.index], errors="ignore"
        )
        valid_days = tv_prev.count()
        valid_coins = valid_days[valid_days >= 100].index
        avg_tv = avg_tv[avg_tv.index.isin(valid_coins)]
        coins = list(avg_tv.head(TOP_N).index)
        universe[y] = coins
        universe_rank[y] = {c: i for i, c in enumerate(coins)}
    return universe, universe_rank

# ─── 지표 ────────────────────────────────────────────────────
def precompute_indicators(close_all, volume_all, universe):
    all_coins = set()
    for coins in universe.values():
        all_coins.update(coins)
    all_coins = list(all_coins & set(close_all.columns))
    indicators = {}
    for coin in all_coins:
        upper, lower, r2 = calc_linear_regression_channel(close_all[coin].values)
        indicators[coin] = {
            "upper": upper,
            "lower": lower,
            "r2": r2,
            "vol_ma": volume_all[coin].rolling(20).mean(),
            "mom5": close_all[coin].pct_change(5),
        }
    return indicators, all_coins

# ─── 달러추적 백테스트 ───────────────────────────────────────
def run_backtest(close_all, volume_all, btc_close,
                 universe, universe_rank, indicators, all_coins):
    btc_sma20 = btc_close.rolling(20).mean()
    btc_sma50 = btc_close.rolling(50).mean()
    market_bullish = btc_sma20 > btc_sma50

    dates = close_all.index
    start_idx = close_all.index.get_loc(close_all.loc[START_DATE:].index[0])

    price_np = {c: close_all[c].values for c in all_coins}

    cash = INITIAL_CAPITAL
    peak_equity = INITIAL_CAPITAL
    mdd_deployed = False
    positions = {}
    trade_log = []
    equity_curve = []
    current_year = None
    current_coins = []
    current_rank = {}

    def get_equity(idx):
        eq = cash
        for coin, (ep, eidx, sk, qty_u, margin) in positions.items():
            cur = price_np[coin][idx]
            if np.isnan(cur):
                eq += margin
                continue
            is_short = STRATS[sk]["direction"] == "short"
            pnl = -(cur / ep - 1) if is_short else (cur / ep - 1)
            eq += margin + qty_u * pnl
        return eq

    for i in range(max(80, start_idx), len(dates)):
        date = dates[i]
        year = date.year

        if year != current_year:
            current_year = year
            current_coins = [c for c in universe.get(year, []) if c in all_coins]
            current_rank = universe_rank.get(year, {})

        is_bull = bool(market_bullish.iloc[i])

        # ── MDD 기반 현금비율 ──
        equity = get_equity(i)
        if equity > peak_equity:
            peak_equity = equity
            mdd_deployed = False
        current_mdd = equity / peak_equity - 1 if peak_equity > 0 else 0
        if current_mdd <= MDD_DEPLOY_THRESH and not mdd_deployed:
            mdd_deployed = True
        effective_cash_ratio = 0.0 if mdd_deployed else CASH_RATIO

        # ── BTC 필터 청산 ──
        for coin in list(positions.keys()):
            ep, eidx, sk, qty_u, margin = positions[coin]
            btcf = STRATS[sk]["btc_filter"]
            if (btcf == "bull" and not is_bull) or (btcf == "bear" and is_bull):
                cur = price_np[coin][i]
                if np.isnan(cur):
                    continue
                is_short = STRATS[sk]["direction"] == "short"
                pnl = -(cur / ep - 1) if is_short else (cur / ep - 1)
                cash += margin + qty_u * pnl
                cash -= qty_u * COST_PER_SIDE
                trade_log.append({
                    "coin": coin, "strat": sk, "pnl": pnl * 100,
                    "held": i - eidx, "reason": "BTC",
                    "dir": STRATS[sk]["direction"],
                })
                del positions[coin]

        # ── SL/TP/TIME 청산 ──
        for coin in list(positions.keys()):
            ep, eidx, sk, qty_u, margin = positions[coin]
            cfg = STRATS[sk]
            cur = price_np[coin][i]
            if np.isnan(cur):
                continue
            is_short = cfg["direction"] == "short"
            pnl = -(cur / ep - 1) if is_short else (cur / ep - 1)
            held = i - eidx

            if held >= cfg["hold_days"] or pnl <= cfg["sl"] or pnl >= cfg["tp"]:
                reason = "TP" if pnl >= cfg["tp"] else "SL" if pnl <= cfg["sl"] else "TIME"
                cash += margin + qty_u * pnl
                cash -= qty_u * COST_PER_SIDE
                trade_log.append({
                    "coin": coin, "strat": sk, "pnl": pnl * 100,
                    "held": held, "reason": reason,
                    "dir": cfg["direction"],
                })
                del positions[coin]

        # ── 진입 ──
        equity = get_equity(i)
        avail_slots = MAX_POS - len(positions)
        all_candidates = []

        if avail_slots > 0:
            for sk, cfg in STRATS.items():
                btcf = cfg["btc_filter"]
                if (btcf == "bull" and not is_bull) or (btcf == "bear" and is_bull):
                    continue
                for coin in current_coins:
                    if coin in positions or coin not in indicators:
                        continue
                    ind = indicators[coin]
                    upper = ind["upper"]
                    lower = ind["lower"]
                    r2_vals = ind["r2"]
                    vol_ma = ind["vol_ma"]

                    if i >= len(upper) or np.isnan(upper[i]) or np.isnan(r2_vals[i]):
                        continue
                    prev_c = price_np[coin][i - 1]
                    curr_c = price_np[coin][i]
                    if np.isnan(prev_c) or np.isnan(curr_c):
                        continue

                    vm = vol_ma.iloc[i]
                    if pd.isna(vm) or vm <= 0:
                        continue
                    vr = volume_all[coin].iloc[i] / vm

                    if r2_vals[i] <= cfg["r2_thresh"] or vr <= cfg["vol_mult"]:
                        continue

                    triggered = False
                    sig = cfg["signal"]
                    if sig == "upper_break" and prev_c <= upper[i] and curr_c > upper[i]:
                        triggered = True
                    elif sig == "lower_break" and prev_c >= lower[i] and curr_c < lower[i]:
                        triggered = True
                    elif sig == "upper_touch" and prev_c < upper[i] and curr_c >= upper[i]:
                        triggered = True

                    if triggered:
                        mom5 = ind["mom5"].iloc[i]
                        mom5 = mom5 if pd.notna(mom5) else 0.01
                        score = r2_vals[i] * vr * max(mom5, 0.01)
                        mcap_rank = current_rank.get(coin, 999)
                        all_candidates.append((coin, sk, score, mcap_rank))

        all_candidates.sort(key=lambda x: (-x[2], x[3]))
        entered = set(positions.keys())

        if all_candidates:
            invest_capital = equity * (1 - effective_cash_ratio)
            new_count = min(avail_slots, len([c for c, _, _, _ in all_candidates if c not in entered]))
            n_total = len(positions) + new_count
            if n_total == 0:
                n_total = 1
            per_slot = invest_capital / n_total
            order_usdt = per_slot * LEVERAGE
            margin = per_slot

            for coin, sk, _, _ in all_candidates:
                if len(positions) >= MAX_POS:
                    break
                if coin in entered:
                    continue
                if cash < margin + order_usdt * COST_PER_SIDE:
                    break

                cash -= margin
                cash -= order_usdt * COST_PER_SIDE
                positions[coin] = (
                    price_np[coin][i], i, sk, order_usdt, margin
                )
                entered.add(coin)

        equity = get_equity(i)
        equity_curve.append(equity)

    return equity_curve, trade_log

# ─── 성과 ────────────────────────────────────────────────────
def print_performance(equity_curve, trade_log, dates_used):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    eq_s = pd.Series(equity_curve, index=dates_used)
    cn = eq_s / eq_s.iloc[0]
    years = len(cn) / 365.0
    final = cn.iloc[-1]
    cagr = (final ** (1.0 / years) - 1.0) * 100 if years > 0 else 0
    dd = cn / cn.expanding().max() - 1.0
    mdd = dd.min() * 100
    calmar = cagr / abs(mdd) if mdd != 0 else 0
    dr = cn.pct_change().dropna()
    sharpe = dr.mean() / dr.std() * np.sqrt(365) if dr.std() > 0 else 0

    tdf = pd.DataFrame(trade_log)
    n_trades = len(tdf)
    wins = len(tdf[tdf["pnl"] > 0]) if n_trades > 0 else 0

    print()
    print("=" * 70)
    print("  채널 돌파 복합 전략 - 달러추적 (3배, 동적1/n)")
    print("=" * 70)
    print(f"  레버리지:    {LEVERAGE}x")
    print(f"  포지션:      동적 1/n (최대 {MAX_POS}슬롯)")
    print(f"  현금:        {CASH_RATIO*100:.0f}% (MDD {MDD_DEPLOY_THRESH*100:.0f}%→전량투입)")
    print(f"  시작자본:    ${INITIAL_CAPITAL:,.0f}")
    print(f"  최종자산:    ${eq_s.iloc[-1]:,.0f}")
    print(f"  CAGR:        {cagr:+,.1f}%")
    print(f"  MDD:         {mdd:.1f}%")
    print(f"  Calmar:      {calmar:.2f}")
    print(f"  Sharpe:      {sharpe:.2f}")
    print(f"  배수:        {final:,.0f}x")
    print(f"  수수료:      편도 {COST_PER_SIDE*100:.2f}%")

    print(f"\n  연도별:")
    print("  " + "-" * 50)
    for year in range(cn.index[0].year, cn.index[-1].year + 1):
        y_data = cn[cn.index.year == year]
        if len(y_data) < 2:
            continue
        y_ret = (y_data.iloc[-1] / y_data.iloc[0] - 1) * 100
        y_mdd = (y_data / y_data.expanding().max() - 1).min() * 100
        print(f"    {year}:  {y_ret:+9.1f}%   MDD {y_mdd:6.1f}%")

    if n_trades > 0:
        print(f"\n  거래: {n_trades}건  승률 {wins/n_trades*100:.0f}%")
        print(f"\n  전략별:")
        for sk in ["A", "B", "C"]:
            t = tdf[tdf["strat"] == sk]
            if len(t) == 0:
                continue
            w = t[t["pnl"] > 0]
            l = t[t["pnl"] <= 0]
            reasons = t["reason"].value_counts().to_dict()
            print(f"    {sk}({STRATS[sk]['name']}): {len(t)}건, "
                  f"승률 {len(w)/len(t)*100:.0f}%, "
                  f"이익 {w['pnl'].mean():+.1f}%, "
                  f"손실 {l['pnl'].mean():+.1f}%, "
                  f"보유 {t['held'].mean():.1f}일")
            print(f"      {reasons}")

    print("=" * 70)

# ─── 메인 ────────────────────────────────────────────────────
if __name__ == "__main__":
    t0 = time.time()
    print("=" * 70)
    print("  채널 돌파 복합 전략 - 달러추적 백테스트")
    print("  원본 ABC | 3배 | 동적1/n | 현금50%→MDD35→0%")
    print("=" * 70)

    # 1. 데이터 (bt_cache.pkl)
    print("\n1. 데이터 로드...")
    close_all, volume_all = load_cache_data()
    btc_close = close_all["BTCUSDT"]
    print(f"  기간: {close_all.index[0].date()} ~ {close_all.index[-1].date()}")
    print(f"  종목: {len(close_all.columns)}개")

    # 2. 유니버스
    print("2. 유니버스 선정...")
    universe, universe_rank = build_annual_universe(close_all, volume_all)
    for y, coins in universe.items():
        print(f"   {y}: {len(coins)}종목")

    # 3. 지표
    print("3. 지표 계산...")
    indicators, all_coins = precompute_indicators(close_all, volume_all, universe)
    print(f"   {len(indicators)}종목")

    # 4. 백테스트
    print("4. 달러추적 백테스트...")
    start_idx = close_all.index.get_loc(close_all.loc[START_DATE:].index[0])
    dates_used = close_all.index[max(80, start_idx):]

    equity_curve, trade_log = run_backtest(
        close_all, volume_all, btc_close,
        universe, universe_rank, indicators, all_coins,
    )

    # 5. 성과
    print_performance(equity_curve, trade_log, dates_used[:len(equity_curve)])
    print(f"\n  완료! ({time.time() - t0:.1f}초)")
