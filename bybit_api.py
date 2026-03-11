"""
바이비트 V5 선물 API 클라이언트
- USDT 퍼페추얼 (linear)
- pybit 공식 SDK 사용
"""
import logging
from pybit.unified_trading import HTTP

log = logging.getLogger(__name__)


class BybitAPI:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        self.session = HTTP(
            api_key=api_key,
            api_secret=api_secret,
            testnet=testnet,
        )
        self.category = "linear"

    # ── 시세 ──────────────────────────────────────────────────

    def get_klines(self, symbol: str, interval: str = "D", limit: int = 50) -> list:
        """일봉 등 OHLCV 조회. interval: 1,3,5,15,30,60,120,240,360,720,D,W,M"""
        r = self.session.get_kline(
            category=self.category,
            symbol=symbol,
            interval=interval,
            limit=limit,
        )
        # 반환: [[ts, open, high, low, close, volume, turnover], ...]
        # 최신순 → 오래된순으로 뒤집기
        return list(reversed(r["result"]["list"]))

    def get_ticker(self, symbol: str) -> dict:
        """현재가 조회"""
        r = self.session.get_tickers(
            category=self.category,
            symbol=symbol,
        )
        return r["result"]["list"][0]

    def get_tickers_all(self) -> list:
        """전 종목 티커 조회"""
        r = self.session.get_tickers(category=self.category)
        return r["result"]["list"]

    # ── 계좌 ──────────────────────────────────────────────────

    def get_balance(self) -> float:
        """USDT 가용 잔고"""
        r = self.session.get_wallet_balance(accountType="UNIFIED")
        for acct in r["result"]["list"]:
            for coin in acct.get("coin", []):
                if coin["coin"] == "USDT":
                    for key in ("availableToWithdraw", "walletBalance", "equity"):
                        val = coin.get(key, "")
                        if val:
                            return float(val)
                    return 0.0
        return 0.0

    def get_equity(self) -> float:
        """총 자산 (USDT 기준)"""
        r = self.session.get_wallet_balance(accountType="UNIFIED")
        for acct in r["result"]["list"]:
            for key in ("totalEquity", "totalMarginBalance", "totalWalletBalance"):
                val = acct.get(key, "")
                if val:
                    return float(val)
        return 0.0

    def get_positions(self) -> list:
        """보유 포지션 조회"""
        r = self.session.get_positions(
            category=self.category,
            settleCoin="USDT",
        )
        positions = []
        for p in r["result"]["list"]:
            size = float(p["size"])
            if size > 0:
                positions.append({
                    "symbol": p["symbol"],
                    "side": p["side"],  # Buy or Sell
                    "size": size,
                    "entry_price": float(p["avgPrice"]),
                    "unrealised_pnl": float(p["unrealisedPnl"]),
                    "leverage": p["leverage"],
                })
        return positions

    # ── 주문 ──────────────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int = 2):
        """레버리지 설정"""
        try:
            self.session.set_leverage(
                category=self.category,
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
            log.info(f"{symbol} 레버리지 {leverage}x 설정")
        except Exception as e:
            # 이미 설정된 경우 에러 무시
            if "not modified" in str(e).lower():
                pass
            else:
                log.warning(f"{symbol} 레버리지 설정 실패: {e}")

    def open_long(self, symbol: str, qty: str) -> dict:
        """롱 포지션 시장가 진입"""
        r = self.session.place_order(
            category=self.category,
            symbol=symbol,
            side="Buy",
            orderType="Market",
            qty=qty,
        )
        log.info(f"롱 진입: {symbol} qty={qty} → {r}")
        return r

    def open_short(self, symbol: str, qty: str) -> dict:
        """숏 포지션 시장가 진입"""
        r = self.session.place_order(
            category=self.category,
            symbol=symbol,
            side="Sell",
            orderType="Market",
            qty=qty,
        )
        log.info(f"숏 진입: {symbol} qty={qty} → {r}")
        return r

    def close_position(self, symbol: str, side: str, qty: str) -> dict:
        """포지션 청산 (반대방향 시장가)"""
        close_side = "Sell" if side == "Buy" else "Buy"
        r = self.session.place_order(
            category=self.category,
            symbol=symbol,
            side=close_side,
            orderType="Market",
            qty=qty,
            reduceOnly=True,
        )
        log.info(f"청산: {symbol} {side}→{close_side} qty={qty} → {r}")
        return r

    # ── 종목 정보 ─────────────────────────────────────────────

    def get_instruments(self) -> dict:
        """거래 가능 종목 + 최소수량/틱사이즈 조회"""
        r = self.session.get_instruments_info(category=self.category)
        instruments = {}
        for item in r["result"]["list"]:
            sym = item["symbol"]
            instruments[sym] = {
                "min_qty": float(item["lotSizeFilter"]["minOrderQty"]),
                "qty_step": float(item["lotSizeFilter"]["qtyStep"]),
                "tick_size": float(item["priceFilter"]["tickSize"]),
                "status": item["status"],
            }
        return instruments
