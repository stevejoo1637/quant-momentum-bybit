#!/usr/bin/env python3
"""
VBT Pro 파라미터 최적화 - 바이비트 채널 돌파 복합 전략
=====================================================
6단계 순차 스윕: SL/TP → 보유일 → 슬롯/현금 → 레버리지 → 필터 → MDD
Calmar ratio 기준 최적 선택
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import numpy as np
import pandas as pd
import pickle
import time
import warnings
warnings.filterwarnings('ignore')

import os

CACHE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(CACHE_DIR, "bt_cache.pkl")
START_DATE = "2023-01-01"
TOP_N = 60
EXCLUDE = {"BTCUSDT", "ETHUSDT"}
INITIAL_CAPITAL = 10000.0

t0 = time.time()

# ═══════════════════════════════════════════════════════════════
# 데이터 로드 & 사전 계산
# ═══════════════════════════════════════════════════════════════
print("=" * 70)
print("  VBT Pro 파라미터 최적화 - 바이비트 채널 돌파 전략 [API 데이터]")
print("=" * 70)

print("\n1. 데이터 로드 (bt_cache.pkl)...")
with open(CACHE_FILE, "rb") as f:
    cache = pickle.load(f)

close_dict, volume_dict = {}, {}
for sym, df in cache.items():
    if df.empty:
        continue
    s = df.set_index("date").sort_index()
    s.index = pd.to_datetime(s.index)
    close_dict[sym] = s["close"]
    volume_dict[sym] = s["volume"]

close_all = pd.DataFrame(close_dict).sort_index()
volume_all = pd.DataFrame(volume_dict).sort_index()
btc_close = close_all["BTCUSDT"]
print(f"  기간: {close_all.index[0].date()} ~ {close_all.index[-1].date()}")
print(f"  종목: {len(close_all.columns)}개")

# BTC 시장 필터 사전 계산
btc_sma20 = btc_close.rolling(20).mean()
btc_sma50 = btc_close.rolling(50).mean()
market_bullish = btc_sma20 > btc_sma50

# 유니버스
print("2. 유니버스 선정...")
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
        tv_prev = turnover.loc[:f"{y-1}-12-31"]
    if len(tv_prev) == 0:
        universe[y] = []
        universe_rank[y] = {}
        continue
    avg_tv = tv_prev.mean().dropna().sort_values(ascending=False)
    avg_tv = avg_tv.drop(labels=[s for s in EXCLUDE if s in avg_tv.index], errors="ignore")
    valid_days = tv_prev.count()
    valid_coins = valid_days[valid_days >= 100].index
    avg_tv = avg_tv[avg_tv.index.isin(valid_coins)]
    coins = list(avg_tv.head(TOP_N).index)
    universe[y] = coins
    universe_rank[y] = {c: i for i, c in enumerate(coins)}
    print(f"   {y}: {len(coins)}종목")

# 채널 지표 사전 계산
print("3. 지표 계산...")
all_coins = set()
for coins in universe.values():
    all_coins.update(coins)
all_coins = list(all_coins & set(close_all.columns))

def calc_channel_vectorized(prices, period=20, std_mult=2.0):
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

indicators = {}
for coin in all_coins:
    upper, lower, r2 = calc_channel_vectorized(close_all[coin].values)
    indicators[coin] = {
        "upper": upper,
        "lower": lower,
        "r2": r2,
        "vol_ma": volume_all[coin].rolling(20).mean(),
        "mom5": close_all[coin].pct_change(5),
    }
print(f"   {len(indicators)}종목 완료")

# numpy 배열 사전 변환
price_np = {c: close_all[c].values for c in all_coins}
dates = close_all.index
start_idx = close_all.index.get_loc(close_all.loc[START_DATE:].index[0])


# ═══════════════════════════════════════════════════════════════
# 파라미터화된 백테스트 엔진
# ═══════════════════════════════════════════════════════════════
def run_opt(strats, max_pos=4, cash_ratio=0.50, leverage=3,
            mdd_thresh=-0.35, cost=0.001):
    """파라미터 주입 백테스트. strats = {A/B/C: {sl, tp, hold_days, r2_thresh, vol_mult, ...}}"""

    cash = INITIAL_CAPITAL
    peak_equity = INITIAL_CAPITAL
    mdd_deployed = False
    positions = {}
    trade_count = 0
    wins = 0
    current_year = None
    current_coins = []
    current_rank = {}
    equity_list = []

    def get_equity(idx):
        eq = cash
        for coin, (ep, eidx, sk, qty_u, margin) in positions.items():
            cur = price_np[coin][idx]
            if np.isnan(cur):
                eq += margin
                continue
            is_short = strats[sk]["direction"] == "short"
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

        equity = get_equity(i)
        if equity > peak_equity:
            peak_equity = equity
            mdd_deployed = False
        current_mdd = equity / peak_equity - 1 if peak_equity > 0 else 0
        if mdd_thresh is not None and current_mdd <= mdd_thresh and not mdd_deployed:
            mdd_deployed = True
        effective_cash_ratio = 0.0 if mdd_deployed else cash_ratio

        # BTC 필터 청산
        for coin in list(positions.keys()):
            ep, eidx, sk, qty_u, margin = positions[coin]
            btcf = strats[sk]["btc_filter"]
            if (btcf == "bull" and not is_bull) or (btcf == "bear" and is_bull):
                cur = price_np[coin][i]
                if np.isnan(cur):
                    continue
                is_short = strats[sk]["direction"] == "short"
                pnl = -(cur / ep - 1) if is_short else (cur / ep - 1)
                cash += margin + qty_u * pnl
                cash -= qty_u * cost
                trade_count += 1
                if pnl > 0:
                    wins += 1
                del positions[coin]

        # SL/TP/TIME 청산
        for coin in list(positions.keys()):
            ep, eidx, sk, qty_u, margin = positions[coin]
            cfg = strats[sk]
            cur = price_np[coin][i]
            if np.isnan(cur):
                continue
            is_short = cfg["direction"] == "short"
            pnl = -(cur / ep - 1) if is_short else (cur / ep - 1)
            held = i - eidx
            sl_val = cfg["sl"]
            tp_val = cfg["tp"]

            if held >= cfg["hold_days"] or pnl <= sl_val or pnl >= tp_val:
                cash += margin + qty_u * pnl
                cash -= qty_u * cost
                trade_count += 1
                if pnl > 0:
                    wins += 1
                del positions[coin]

        # 진입
        equity = get_equity(i)
        avail_slots = max_pos - len(positions)
        all_candidates = []

        if avail_slots > 0:
            for sk, cfg in strats.items():
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
            order_usdt = per_slot * leverage
            margin = per_slot

            for coin, sk, _, _ in all_candidates:
                if len(positions) >= max_pos:
                    break
                if coin in entered:
                    continue
                if cash < margin + order_usdt * cost:
                    break
                cash -= margin
                cash -= order_usdt * cost
                positions[coin] = (price_np[coin][i], i, sk, order_usdt, margin)
                entered.add(coin)

        equity = get_equity(i)
        equity_list.append(equity)

    # 성과 계산
    eq_arr = np.array(equity_list)
    if len(eq_arr) < 2 or eq_arr[0] <= 0:
        return {"cagr": -999, "mdd": -100, "calmar": -999, "sharpe": 0, "trades": 0, "winrate": 0, "final": 0}

    final_mult = eq_arr[-1] / eq_arr[0]
    years = len(eq_arr) / 365.0
    cagr = (final_mult ** (1.0 / years) - 1.0) * 100 if years > 0 and final_mult > 0 else -999

    peak = np.maximum.accumulate(eq_arr)
    dd = eq_arr / peak - 1
    mdd = dd.min() * 100

    dr = np.diff(eq_arr) / eq_arr[:-1]
    sharpe = dr.mean() / dr.std() * np.sqrt(365) if dr.std() > 0 else 0

    calmar = cagr / abs(mdd) if mdd != 0 else 0
    winrate = wins / trade_count * 100 if trade_count > 0 else 0

    return {
        "cagr": cagr, "mdd": mdd, "calmar": calmar, "sharpe": sharpe,
        "trades": trade_count, "winrate": winrate, "final": eq_arr[-1],
    }


# ═══════════════════════════════════════════════════════════════
# 기본 전략 설정 (현재 라이브)
# ═══════════════════════════════════════════════════════════════
def make_strats(a_sl=-0.07, a_tp=0.25, a_hd=7, a_r2=0.5, a_vm=1.5,
                b_sl=-0.05, b_tp=0.15, b_hd=14, b_r2=0.5, b_vm=1.0,
                c_sl=-0.15, c_tp=0.20, c_hd=10, c_r2=0.3, c_vm=1.0):
    return {
        "A": {
            "name": "상단돌파 롱", "signal": "upper_break", "direction": "long",
            "btc_filter": "bull", "sl": a_sl, "tp": a_tp, "hold_days": a_hd,
            "r2_thresh": a_r2, "vol_mult": a_vm,
        },
        "B": {
            "name": "하단돌파 롱", "signal": "lower_break", "direction": "long",
            "btc_filter": "none", "sl": b_sl, "tp": b_tp, "hold_days": b_hd,
            "r2_thresh": b_r2, "vol_mult": b_vm,
        },
        "C": {
            "name": "상단터치 숏", "signal": "upper_touch", "direction": "short",
            "btc_filter": "bear", "sl": c_sl, "tp": c_tp, "hold_days": c_hd,
            "r2_thresh": c_r2, "vol_mult": c_vm,
        },
    }


def print_table(results, key_fmt):
    best_cal = max(r['calmar'] for r in results.values())
    print(f"\n{'파라미터':>18} {'CAGR':>9} {'MDD':>8} {'Sharpe':>8} {'Calmar':>8} {'거래':>5} {'승률':>5}")
    print("-" * 70)
    for k in sorted(results.keys(), key=lambda x: str(x)):
        r = results[k]
        mk = " ★" if r['calmar'] == best_cal else ""
        label = key_fmt(k)
        print(f"{label:>18} {r['cagr']:>+8.1f}% {r['mdd']:>7.1f}% {r['sharpe']:>8.2f} {r['calmar']:>8.2f} {r['trades']:>5} {r['winrate']:>4.0f}%{mk}")


# ═══════════════════════════════════════════════════════════════
# 기준선: 현재 설정
# ═══════════════════════════════════════════════════════════════
print("\n4. 기준선 (현재 라이브 설정)...")
baseline = run_opt(make_strats(), max_pos=4, cash_ratio=0.50, leverage=3, mdd_thresh=-0.35)
print(f"  CAGR: {baseline['cagr']:+.1f}%, MDD: {baseline['mdd']:.1f}%, "
      f"Calmar: {baseline['calmar']:.2f}, Sharpe: {baseline['sharpe']:.2f}, "
      f"거래: {baseline['trades']}건, 승률: {baseline['winrate']:.0f}%")
print(f"  최종자산: ${baseline['final']:,.0f}")

# 최적 파라미터 추적
best = {
    "a_sl": -0.07, "a_tp": 0.25, "a_hd": 7, "a_r2": 0.5, "a_vm": 1.5,
    "b_sl": -0.05, "b_tp": 0.15, "b_hd": 14, "b_r2": 0.5, "b_vm": 1.0,
    "c_sl": -0.15, "c_tp": 0.20, "c_hd": 10, "c_r2": 0.3, "c_vm": 1.0,
    "max_pos": 4, "cash_ratio": 0.50, "leverage": 3, "mdd_thresh": -0.35,
}


# ═══════════════════════════════════════════════════════════════
# [Stage 1] 전략별 SL/TP 스윕
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("[Stage 1] 전략별 SL/TP 스윕")
print("=" * 70)

# ── A: 상단돌파 롱 ──
print("\n[1-A] 상단돌파 롱 SL/TP")
res_a = {}
for sl in [0.05, 0.07, 0.10, 0.12]:
    for tp in [0.15, 0.20, 0.25, 0.30, 0.40]:
        s = make_strats(a_sl=-sl, a_tp=tp)
        r = run_opt(s, max_pos=best["max_pos"], cash_ratio=best["cash_ratio"],
                    leverage=best["leverage"], mdd_thresh=best["mdd_thresh"])
        res_a[(sl, tp)] = r
        print(f"  SL{sl:.0%}/TP{tp:.0%}: CAGR {r['cagr']:+.1f}% MDD {r['mdd']:.1f}% Calmar {r['calmar']:.2f}")

print_table(res_a, lambda k: f"SL{k[0]:.0%}/TP{k[1]:.0%}")
b_a = max(res_a, key=lambda k: res_a[k]['calmar'])
best["a_sl"] = -b_a[0]
best["a_tp"] = b_a[1]
print(f"  → A 최적: SL {b_a[0]:.0%}, TP {b_a[1]:.0%}")

# ── B: 하단돌파 롱 ──
print("\n[1-B] 하단돌파 롱 SL/TP")
res_b = {}
for sl in [0.03, 0.05, 0.07, 0.10]:
    for tp in [0.10, 0.15, 0.20, 0.25]:
        s = make_strats(a_sl=best["a_sl"], a_tp=best["a_tp"], b_sl=-sl, b_tp=tp)
        r = run_opt(s, max_pos=best["max_pos"], cash_ratio=best["cash_ratio"],
                    leverage=best["leverage"], mdd_thresh=best["mdd_thresh"])
        res_b[(sl, tp)] = r
        print(f"  SL{sl:.0%}/TP{tp:.0%}: CAGR {r['cagr']:+.1f}% MDD {r['mdd']:.1f}% Calmar {r['calmar']:.2f}")

print_table(res_b, lambda k: f"SL{k[0]:.0%}/TP{k[1]:.0%}")
b_b = max(res_b, key=lambda k: res_b[k]['calmar'])
best["b_sl"] = -b_b[0]
best["b_tp"] = b_b[1]
print(f"  → B 최적: SL {b_b[0]:.0%}, TP {b_b[1]:.0%}")

# ── C: 상단터치 숏 ──
print("\n[1-C] 상단터치 숏 SL/TP")
res_c = {}
for sl in [0.08, 0.10, 0.12, 0.15, 0.20]:
    for tp in [0.15, 0.20, 0.25, 0.30]:
        s = make_strats(a_sl=best["a_sl"], a_tp=best["a_tp"],
                        b_sl=best["b_sl"], b_tp=best["b_tp"],
                        c_sl=-sl, c_tp=tp)
        r = run_opt(s, max_pos=best["max_pos"], cash_ratio=best["cash_ratio"],
                    leverage=best["leverage"], mdd_thresh=best["mdd_thresh"])
        res_c[(sl, tp)] = r
        print(f"  SL{sl:.0%}/TP{tp:.0%}: CAGR {r['cagr']:+.1f}% MDD {r['mdd']:.1f}% Calmar {r['calmar']:.2f}")

print_table(res_c, lambda k: f"SL{k[0]:.0%}/TP{k[1]:.0%}")
b_c = max(res_c, key=lambda k: res_c[k]['calmar'])
best["c_sl"] = -b_c[0]
best["c_tp"] = b_c[1]
print(f"  → C 최적: SL {b_c[0]:.0%}, TP {b_c[1]:.0%}")


# ═══════════════════════════════════════════════════════════════
# [Stage 2] 전략별 보유일 스윕
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("[Stage 2] 전략별 보유일 스윕")
print("=" * 70)

# A 보유일
print("\n[2-A] 상단돌파 롱 보유일")
res_hd_a = {}
for hd in [3, 5, 7, 10, 14]:
    s = make_strats(a_sl=best["a_sl"], a_tp=best["a_tp"], a_hd=hd,
                    b_sl=best["b_sl"], b_tp=best["b_tp"],
                    c_sl=best["c_sl"], c_tp=best["c_tp"])
    r = run_opt(s, max_pos=best["max_pos"], cash_ratio=best["cash_ratio"],
                leverage=best["leverage"], mdd_thresh=best["mdd_thresh"])
    res_hd_a[hd] = r
    print(f"  {hd}일: CAGR {r['cagr']:+.1f}% MDD {r['mdd']:.1f}% Calmar {r['calmar']:.2f}")

print_table(res_hd_a, lambda k: f"{k}일")
best["a_hd"] = max(res_hd_a, key=lambda k: res_hd_a[k]['calmar'])
print(f"  → A 최적: {best['a_hd']}일")

# B 보유일
print("\n[2-B] 하단돌파 롱 보유일")
res_hd_b = {}
for hd in [7, 10, 14, 20, 30]:
    s = make_strats(a_sl=best["a_sl"], a_tp=best["a_tp"], a_hd=best["a_hd"],
                    b_sl=best["b_sl"], b_tp=best["b_tp"], b_hd=hd,
                    c_sl=best["c_sl"], c_tp=best["c_tp"])
    r = run_opt(s, max_pos=best["max_pos"], cash_ratio=best["cash_ratio"],
                leverage=best["leverage"], mdd_thresh=best["mdd_thresh"])
    res_hd_b[hd] = r
    print(f"  {hd}일: CAGR {r['cagr']:+.1f}% MDD {r['mdd']:.1f}% Calmar {r['calmar']:.2f}")

print_table(res_hd_b, lambda k: f"{k}일")
best["b_hd"] = max(res_hd_b, key=lambda k: res_hd_b[k]['calmar'])
print(f"  → B 최적: {best['b_hd']}일")

# C 보유일
print("\n[2-C] 상단터치 숏 보유일")
res_hd_c = {}
for hd in [5, 7, 10, 14, 20]:
    s = make_strats(a_sl=best["a_sl"], a_tp=best["a_tp"], a_hd=best["a_hd"],
                    b_sl=best["b_sl"], b_tp=best["b_tp"], b_hd=best["b_hd"],
                    c_sl=best["c_sl"], c_tp=best["c_tp"], c_hd=hd)
    r = run_opt(s, max_pos=best["max_pos"], cash_ratio=best["cash_ratio"],
                leverage=best["leverage"], mdd_thresh=best["mdd_thresh"])
    res_hd_c[hd] = r
    print(f"  {hd}일: CAGR {r['cagr']:+.1f}% MDD {r['mdd']:.1f}% Calmar {r['calmar']:.2f}")

print_table(res_hd_c, lambda k: f"{k}일")
best["c_hd"] = max(res_hd_c, key=lambda k: res_hd_c[k]['calmar'])
print(f"  → C 최적: {best['c_hd']}일")


# ═══════════════════════════════════════════════════════════════
# [Stage 3] 슬롯 수 / 현금비율 스윕
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("[Stage 3] 슬롯/현금비율 스윕")
print("=" * 70)

res3 = {}
s = make_strats(a_sl=best["a_sl"], a_tp=best["a_tp"], a_hd=best["a_hd"],
                b_sl=best["b_sl"], b_tp=best["b_tp"], b_hd=best["b_hd"],
                c_sl=best["c_sl"], c_tp=best["c_tp"], c_hd=best["c_hd"])

for mp in [3, 4, 5, 6]:
    for cr in [0.30, 0.40, 0.50, 0.60]:
        r = run_opt(s, max_pos=mp, cash_ratio=cr,
                    leverage=best["leverage"], mdd_thresh=best["mdd_thresh"])
        res3[(mp, cr)] = r
        print(f"  {mp}슬롯/{cr:.0%}현금: CAGR {r['cagr']:+.1f}% MDD {r['mdd']:.1f}% Calmar {r['calmar']:.2f}")

print_table(res3, lambda k: f"{k[0]}슬롯/{k[1]:.0%}")
b3 = max(res3, key=lambda k: res3[k]['calmar'])
best["max_pos"] = b3[0]
best["cash_ratio"] = b3[1]
print(f"  → 최적: {b3[0]}슬롯, {b3[1]:.0%} 현금")


# ═══════════════════════════════════════════════════════════════
# [Stage 4] 레버리지 스윕
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("[Stage 4] 레버리지 스윕")
print("=" * 70)

res4 = {}
for lev in [1, 2, 3, 4, 5]:
    r = run_opt(s, max_pos=best["max_pos"], cash_ratio=best["cash_ratio"],
                leverage=lev, mdd_thresh=best["mdd_thresh"])
    res4[lev] = r
    print(f"  {lev}x: CAGR {r['cagr']:+.1f}% MDD {r['mdd']:.1f}% Calmar {r['calmar']:.2f}")

print_table(res4, lambda k: f"{k}x")
best["leverage"] = max(res4, key=lambda k: res4[k]['calmar'])
print(f"  → 최적: {best['leverage']}x")


# ═══════════════════════════════════════════════════════════════
# [Stage 5] R²/볼륨 필터 스윕
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("[Stage 5] R²/볼륨 필터 스윕")
print("=" * 70)

# A 볼륨 배수 (가장 영향 큼)
print("\n[5-A] A전략 볼륨 배수")
res5a = {}
for vm in [1.0, 1.5, 2.0, 2.5, 3.0]:
    s5 = make_strats(a_sl=best["a_sl"], a_tp=best["a_tp"], a_hd=best["a_hd"], a_vm=vm,
                     b_sl=best["b_sl"], b_tp=best["b_tp"], b_hd=best["b_hd"],
                     c_sl=best["c_sl"], c_tp=best["c_tp"], c_hd=best["c_hd"])
    r = run_opt(s5, max_pos=best["max_pos"], cash_ratio=best["cash_ratio"],
                leverage=best["leverage"], mdd_thresh=best["mdd_thresh"])
    res5a[vm] = r
    print(f"  {vm:.1f}x: CAGR {r['cagr']:+.1f}% MDD {r['mdd']:.1f}% Calmar {r['calmar']:.2f}")

print_table(res5a, lambda k: f"{k:.1f}x")
best["a_vm"] = max(res5a, key=lambda k: res5a[k]['calmar'])
print(f"  → A 볼륨 최적: {best['a_vm']:.1f}x")

# R² 임계값 (A,B 공통)
print("\n[5-R²] A/B R² 임계값")
res5r = {}
for r2t in [0.3, 0.4, 0.5, 0.6, 0.7]:
    s5 = make_strats(a_sl=best["a_sl"], a_tp=best["a_tp"], a_hd=best["a_hd"],
                     a_vm=best["a_vm"], a_r2=r2t,
                     b_sl=best["b_sl"], b_tp=best["b_tp"], b_hd=best["b_hd"], b_r2=r2t,
                     c_sl=best["c_sl"], c_tp=best["c_tp"], c_hd=best["c_hd"])
    r = run_opt(s5, max_pos=best["max_pos"], cash_ratio=best["cash_ratio"],
                leverage=best["leverage"], mdd_thresh=best["mdd_thresh"])
    res5r[r2t] = r
    print(f"  R²>{r2t:.1f}: CAGR {r['cagr']:+.1f}% MDD {r['mdd']:.1f}% Calmar {r['calmar']:.2f}")

print_table(res5r, lambda k: f"R²>{k:.1f}")
best_r2 = max(res5r, key=lambda k: res5r[k]['calmar'])
best["a_r2"] = best_r2
best["b_r2"] = best_r2
print(f"  → R² 최적: >{best_r2:.1f}")


# ═══════════════════════════════════════════════════════════════
# [Stage 6] MDD 전량투입 임계값
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("[Stage 6] MDD 전량투입 임계값")
print("=" * 70)

res6 = {}
s6 = make_strats(a_sl=best["a_sl"], a_tp=best["a_tp"], a_hd=best["a_hd"],
                 a_vm=best["a_vm"], a_r2=best["a_r2"],
                 b_sl=best["b_sl"], b_tp=best["b_tp"], b_hd=best["b_hd"], b_r2=best["b_r2"],
                 c_sl=best["c_sl"], c_tp=best["c_tp"], c_hd=best["c_hd"])

for mt in [-0.20, -0.25, -0.30, -0.35, -0.40, -0.50, None]:
    r = run_opt(s6, max_pos=best["max_pos"], cash_ratio=best["cash_ratio"],
                leverage=best["leverage"], mdd_thresh=mt)
    label = "없음" if mt is None else mt
    res6[label] = r
    mt_str = "없음" if mt is None else f"{mt:.0%}"
    print(f"  MDD {mt_str}: CAGR {r['cagr']:+.1f}% MDD {r['mdd']:.1f}% Calmar {r['calmar']:.2f}")

print_table(res6, lambda k: str(k) if k == "없음" else f"MDD{k:.0%}")
b6 = max(res6, key=lambda k: res6[k]['calmar'])
best["mdd_thresh"] = None if b6 == "없음" else b6
print(f"  → 최적: {'없음' if b6 == '없음' else f'MDD {b6:.0%}'}")


# ═══════════════════════════════════════════════════════════════
# [최종] 결과
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  [최종] VBT Pro 최적 파라미터")
print("=" * 70)

print(f"\n  A(상단돌파 롱): SL {-best['a_sl']:.0%}, TP {best['a_tp']:.0%}, "
      f"보유 {best['a_hd']}일, R²>{best['a_r2']:.1f}, 볼륨 {best['a_vm']:.1f}x")
print(f"  B(하단돌파 롱): SL {-best['b_sl']:.0%}, TP {best['b_tp']:.0%}, "
      f"보유 {best['b_hd']}일, R²>{best['b_r2']:.1f}, 볼륨 1.0x")
print(f"  C(상단터치 숏): SL {-best['c_sl']:.0%}, TP {best['c_tp']:.0%}, "
      f"보유 {best['c_hd']}일, R²>0.3, 볼륨 1.0x")
print(f"  슬롯: {best['max_pos']}, 현금: {best['cash_ratio']:.0%}, "
      f"레버리지: {best['leverage']}x")
mdd_str = "없음" if best["mdd_thresh"] is None else f"{best['mdd_thresh']:.0%}"
print(f"  MDD 전량투입: {mdd_str}")

# 최적 파라미터로 최종 실행
s_final = make_strats(
    a_sl=best["a_sl"], a_tp=best["a_tp"], a_hd=best["a_hd"],
    a_vm=best["a_vm"], a_r2=best["a_r2"],
    b_sl=best["b_sl"], b_tp=best["b_tp"], b_hd=best["b_hd"], b_r2=best["b_r2"],
    c_sl=best["c_sl"], c_tp=best["c_tp"], c_hd=best["c_hd"],
)
r_final = run_opt(s_final, max_pos=best["max_pos"], cash_ratio=best["cash_ratio"],
                  leverage=best["leverage"], mdd_thresh=best["mdd_thresh"])

print(f"\n  ── 최적화 결과 ──")
print(f"  CAGR:     {r_final['cagr']:+,.1f}%")
print(f"  MDD:      {r_final['mdd']:.1f}%")
print(f"  Calmar:   {r_final['calmar']:.2f}")
print(f"  Sharpe:   {r_final['sharpe']:.2f}")
print(f"  거래:     {r_final['trades']}건")
print(f"  승률:     {r_final['winrate']:.0f}%")
print(f"  최종자산: ${r_final['final']:,.0f}")

print(f"\n  ── 기준선 (현재 라이브) ──")
print(f"  CAGR:     {baseline['cagr']:+,.1f}%")
print(f"  MDD:      {baseline['mdd']:.1f}%")
print(f"  Calmar:   {baseline['calmar']:.2f}")
print(f"  Sharpe:   {baseline['sharpe']:.2f}")
print(f"  최종자산: ${baseline['final']:,.0f}")

print(f"\n  ── 개선 ──")
print(f"  CAGR:   {r_final['cagr'] - baseline['cagr']:+.1f}%p")
print(f"  MDD:    {r_final['mdd'] - baseline['mdd']:+.1f}%p")
print(f"  Calmar: {r_final['calmar'] - baseline['calmar']:+.2f}")

print("\n" + "=" * 70)
print(f"  완료! ({time.time()-t0:.1f}초)")
