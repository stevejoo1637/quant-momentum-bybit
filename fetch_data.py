#!/usr/bin/env python3
"""
Bybit 일봉 데이터 다운로드
- 선물(linear) 거래대금 상위 종목 자동 선별
- 4년치 일봉 OHLCV 저장
"""

import os
import json
import time
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

BASE_URL = "https://api.bybit.com"
EXCLUDE = {"BTCUSDT", "ETHUSDT"}
TOP_N = 80  # 넉넉하게 다운로드
DAYS = 1460  # 4년


def get_tickers():
    """선물 티커 전체 조회"""
    url = f"{BASE_URL}/v5/market/tickers?category=linear"
    r = requests.get(url, timeout=15)
    data = r.json()
    if data["retCode"] != 0:
        raise Exception(f"tickers error: {data['retMsg']}")
    return data["result"]["list"]


def get_instruments():
    """종목 정보 (상장일 등)"""
    url = f"{BASE_URL}/v5/market/instruments-info?category=linear&limit=1000"
    r = requests.get(url, timeout=15)
    data = r.json()
    if data["retCode"] != 0:
        raise Exception(f"instruments error: {data['retMsg']}")
    return {item["symbol"]: item for item in data["result"]["list"]}


def get_klines(symbol, interval="D", start_ms=None, end_ms=None, limit=200):
    """일봉 데이터 조회 (최대 200개)"""
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    }
    if start_ms:
        params["start"] = start_ms
    if end_ms:
        params["end"] = end_ms

    r = requests.get(f"{BASE_URL}/v5/market/kline", params=params, timeout=15)
    data = r.json()
    if data["retCode"] != 0:
        raise Exception(f"kline error {symbol}: {data['retMsg']}")
    return data["result"]["list"]


def fetch_all_klines(symbol, days=DAYS):
    """여러 번 호출하여 전체 기간 데이터 수집"""
    end_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ts = end_ts - days * 24 * 3600 * 1000

    all_rows = []
    cursor_end = end_ts

    while cursor_end > start_ts:
        try:
            rows = get_klines(symbol, interval="D", end_ms=cursor_end, limit=200)
        except Exception as e:
            print(f"  {symbol} error: {e}")
            time.sleep(2)
            try:
                rows = get_klines(symbol, interval="D", end_ms=cursor_end, limit=200)
            except Exception:
                break

        if not rows:
            break

        all_rows.extend(rows)

        # Bybit kline은 최신순 → 마지막(가장 오래된) 타임스탬프
        oldest_ts = int(rows[-1][0])
        if oldest_ts >= cursor_end:
            break
        cursor_end = oldest_ts - 1

        time.sleep(0.15)

    if not all_rows:
        return pd.DataFrame()

    # 중복 제거 & 정렬
    df = pd.DataFrame(all_rows, columns=[
        "timestamp", "open", "high", "low", "close", "volume", "turnover"
    ])
    df["timestamp"] = df["timestamp"].astype(int)
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)

    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        df[col] = df[col].astype(float)

    df["date"] = pd.to_datetime(df["timestamp"], unit="ms").dt.strftime("%Y-%m-%d")

    # 기간 필터
    start_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    df = df[df["date"] >= start_date]

    return df


def select_universe():
    """거래대금 상위 종목 선별"""
    print("티커 조회 중...")
    tickers = get_tickers()

    usdt_perps = [
        t for t in tickers
        if t["symbol"].endswith("USDT") and t["symbol"] not in EXCLUDE
    ]

    ranked = sorted(usdt_perps, key=lambda t: float(t.get("turnover24h", 0)), reverse=True)

    print("종목 정보 조회 중...")
    instruments = get_instruments()

    universe = []
    for t in ranked:
        sym = t["symbol"]
        turnover = float(t.get("turnover24h", 0))
        inst = instruments.get(sym)
        if not inst:
            continue
        if inst.get("status") != "Trading":
            continue

        # 상장일 체크
        launch_ts = inst.get("launchTime", "0")
        if launch_ts and launch_ts != "0":
            launch_date = datetime.fromtimestamp(int(launch_ts) / 1000, tz=timezone.utc)
            days_listed = (datetime.now(timezone.utc) - launch_date).days
            if days_listed < 150:
                continue

        universe.append({"symbol": sym, "turnover24h": turnover})
        if len(universe) >= TOP_N:
            break

    print(f"유니버스: {len(universe)}종목")
    return universe


def main():
    universe = select_universe()

    # 유니버스 저장
    with open(os.path.join(DATA_DIR, "universe.json"), "w") as f:
        json.dump(universe, f, indent=2)

    # BTC 데이터 (시장 필터용)
    print("\nBTC 데이터 다운로드...")
    btc_df = fetch_all_klines("BTCUSDT", days=DAYS)
    if not btc_df.empty:
        btc_df.to_csv(os.path.join(DATA_DIR, "BTCUSDT.csv"), index=False)
        print(f"  BTCUSDT: {len(btc_df)}일")

    # 유니버스 종목 데이터
    total = len(universe)
    for i, item in enumerate(universe):
        sym = item["symbol"]
        print(f"\n[{i+1}/{total}] {sym} 다운로드 중...")

        df = fetch_all_klines(sym, days=DAYS)
        if df.empty:
            print(f"  {sym}: 데이터 없음")
            continue

        df.to_csv(os.path.join(DATA_DIR, f"{sym}.csv"), index=False)
        print(f"  {sym}: {len(df)}일")

        time.sleep(0.2)

    print(f"\n완료! 데이터 저장 위치: {DATA_DIR}/")


if __name__ == "__main__":
    main()
