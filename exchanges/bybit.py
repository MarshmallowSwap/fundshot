"""
exchanges/bybit.py — Funding King SaaS
Implementazione Bybit di ExchangeClient.
Migrazione completa di bybit_client.py con interfaccia astratta.
"""

import asyncio
import logging
import time
from typing import Optional

from pybit.unified_trading import HTTP

from .base import ExchangeClient
from .models import (
    FundingTicker, Position, WalletBalance,
    InstrumentInfo, OrderResult,
)

logger = logging.getLogger(__name__)

EXCLUDED_AUTO_INTERVAL = {
    "BTCUSDT", "BTCUSDC", "BTCUSD",
    "ETHUSDT", "ETHUSDC", "ETHUSD",
    "ETHBTCUSDT", "ETHWUSDT",
}


class BybitClient(ExchangeClient):

    EXCHANGE_ID = "bybit"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        demo: bool = True,
        testnet: bool = False,
        **kwargs,
    ):
        super().__init__(api_key, api_secret, demo, testnet)
        self._session: Optional[HTTP] = None
        self._init_session()

    def _init_session(self) -> None:
        if self.api_key and self.api_secret:
            self._session = HTTP(
                testnet=self.testnet,
                demo=self.demo,
                api_key=self.api_key,
                api_secret=self.api_secret,
            )
        else:
            self._session = HTTP(testnet=self.testnet)
        logger.info("Bybit session inizializzata (%s)", repr(self))

    def reload_session(self) -> None:
        """Ricrea la sessione (es. dopo cambio credenziali)."""
        self._init_session()

    async def _run(self, fn, *args, **kwargs):
        """Esegue una chiamata pybit sincrona in thread separato."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    # ── MONITORING ────────────────────────────────────────────────────────────

    async def get_funding_tickers(self) -> list[FundingTicker]:
        try:
            res = await self._run(self._session.get_tickers, category="linear")
            if res.get("retCode") != 0:
                logger.error("get_tickers error: %s", res.get("retMsg"))
                return []
            tickers = []
            for t in res["result"]["list"]:
                sym = t.get("symbol", "")
                if not sym.endswith("USDT"):
                    continue
                rate = self._sf(t.get("fundingRate"))
                if rate == 0:
                    continue
                tickers.append(FundingTicker(
                    symbol=sym,
                    funding_rate=rate,
                    next_funding_time=int(t.get("nextFundingTime", 0)),
                    funding_interval_h=self._sf(t.get("fundingIntervalHour", 8)),
                    last_price=self._sf(t.get("lastPrice")),
                    price_24h_pct=self._sf(t.get("price24hPcnt")),
                    prev_price_1h=self._sf(t.get("prevPrice1h")),
                    exchange=self.EXCHANGE_ID,
                ))
            return tickers
        except Exception as e:
            logger.error("get_funding_tickers: %s", e)
            return []

    async def get_funding_history(
        self, symbol: str, limit: int = 8
    ) -> list[dict]:
        try:
            res = await self._run(
                self._session.get_funding_rate_history,
                category="linear",
                symbol=symbol,
                limit=limit,
            )
            if res.get("retCode") != 0:
                logger.error("get_funding_history error: %s", res.get("retMsg"))
                return []
            return res["result"]["list"]
        except Exception as e:
            logger.error("get_funding_history %s: %s", symbol, e)
            return []

    async def get_funding_history_7d(self, symbol: str) -> list[dict]:
        now_ms   = int(time.time() * 1000)
        start_ms = now_ms - 7 * 24 * 3600 * 1000
        all_entries: list[dict] = []
        cursor = ""
        while True:
            try:
                kwargs = {
                    "category":  "linear",
                    "symbol":    symbol,
                    "startTime": str(start_ms),
                    "endTime":   str(now_ms),
                    "limit":     200,
                }
                if cursor:
                    kwargs["cursor"] = cursor
                res = await self._run(
                    self._session.get_funding_rate_history, **kwargs
                )
                if res.get("retCode") != 0:
                    break
                entries = res["result"].get("list", [])
                all_entries.extend(entries)
                cursor = res["result"].get("nextPageCursor", "")
                if not cursor or not entries:
                    break
            except Exception as e:
                logger.error("get_funding_history_7d %s: %s", symbol, e)
                break
        return all_entries

    async def get_instruments_info(self) -> dict[str, InstrumentInfo]:
        result = {}
        cursor = ""
        while True:
            try:
                kwargs = {"category": "linear", "limit": 500}
                if cursor:
                    kwargs["cursor"] = cursor
                res = await self._run(
                    self._session.get_instruments_info, **kwargs
                )
                if res.get("retCode") != 0:
                    break
                data = res["result"]
                for item in data.get("list", []):
                    sym = item.get("symbol", "")
                    if not sym.endswith("USDT"):
                        continue
                    try:
                        lot = item.get("lotSizeFilter", {})
                        result[sym] = InstrumentInfo(
                            symbol=sym,
                            funding_interval_min=int(item.get("fundingInterval", 480)),
                            upper_funding_rate=float(item.get("upperFundingRate", 0.00375)),
                            lower_funding_rate=float(item.get("lowerFundingRate", -0.00375)),
                            min_order_qty=self._sf(lot.get("minOrderQty")),
                            qty_step=self._sf(lot.get("qtyStep")),
                            exchange=self.EXCHANGE_ID,
                        )
                    except (ValueError, TypeError):
                        pass
                cursor = data.get("nextPageCursor", "")
                if not cursor:
                    break
            except Exception as e:
                logger.error("get_instruments_info: %s", e)
                break
        logger.info("Bybit instruments info: %d simboli", len(result))
        return result

    # ── ACCOUNT ───────────────────────────────────────────────────────────────

    async def get_wallet_balance(self) -> Optional[WalletBalance]:
        try:
            res = await self._run(
                self._session.get_wallet_balance, accountType="UNIFIED"
            )
            if res.get("retCode") != 0:
                return None
            accounts = res["result"]["list"]
            if not accounts:
                return None
            acc = accounts[0]
            coins = [
                {
                    "coin":          c["coin"],
                    "walletBalance": self._sf(c.get("walletBalance")),
                    "usdValue":      self._sf(c.get("usdValue")),
                    "unrealisedPnl": self._sf(c.get("unrealisedPnl")),
                }
                for c in acc.get("coin", [])
                if self._sf(c.get("walletBalance")) != 0
            ]
            return WalletBalance(
                total_equity=self._sf(acc.get("totalEquity")),
                total_wallet_balance=self._sf(acc.get("totalWalletBalance")),
                total_available_balance=self._sf(acc.get("totalAvailableBalance")),
                total_perp_upl=self._sf(acc.get("totalPerpUPL")),
                total_margin_balance=self._sf(acc.get("totalMarginBalance")),
                coins=coins,
                exchange=self.EXCHANGE_ID,
            )
        except Exception as e:
            logger.error("get_wallet_balance: %s", e)
            return None

    async def get_positions(self) -> list[Position]:
        positions = []
        queries = [
            {"category": "linear", "settleCoin": "USDT", "limit": 200},
            {"category": "linear", "settleCoin": "USDC", "limit": 200},
            {"category": "inverse", "limit": 200},
        ]
        for kwargs in queries:
            cat = kwargs["category"]
            try:
                res = await self._run(self._session.get_positions, **kwargs)
                if res.get("retCode") != 0:
                    continue
                for p in res["result"]["list"]:
                    size = self._sf(p.get("size"))
                    if size == 0:
                        continue
                    position_im    = self._sf(p.get("positionIM"))
                    unrealised_pnl = self._sf(p.get("unrealisedPnl"))
                    pnl_pct = (
                        unrealised_pnl / position_im * 100
                        if position_im else 0
                    )
                    positions.append(Position(
                        symbol=p.get("symbol", "?"),
                        side=p.get("side", "None"),
                        size=size,
                        avg_price=self._sf(p.get("avgPrice")),
                        mark_price=self._sf(p.get("markPrice")),
                        leverage=self._sf(p.get("leverage")),
                        unrealised_pnl=unrealised_pnl,
                        pnl_pct=pnl_pct,
                        position_im=position_im,
                        liq_price=self._sf(p.get("liqPrice")),
                        take_profit=self._sf(p.get("takeProfit")),
                        stop_loss=self._sf(p.get("stopLoss")),
                        cur_realised_pnl=self._sf(p.get("curRealisedPnl")),
                        exchange=self.EXCHANGE_ID,
                    ))
            except Exception as e:
                logger.error("get_positions [%s]: %s", cat, e)
        return positions

    # ── TRADING ───────────────────────────────────────────────────────────────

    async def open_position(
        self,
        symbol: str,
        side: str,
        qty: float,
        leverage: int,
        sl_pct: float = 0.0,
        tp_pct: float = 0.0,
    ) -> OrderResult:
        try:
            # Imposta leva
            await self._run(
                self._session.set_leverage,
                category="linear",
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
            params = dict(
                category="linear",
                symbol=symbol,
                side=side,
                orderType="Market",
                qty=str(qty),
                timeInForce="IOC",
            )
            res = await self._run(self._session.place_order, **params)
            code = res.get("retCode", -1)
            if code != 0:
                return OrderResult(
                    ok=False, symbol=symbol, side=side, qty=qty,
                    error=res.get("retMsg", "unknown"), exchange=self.EXCHANGE_ID,
                )
            order_id = res.get("result", {}).get("orderId", "")
            return OrderResult(
                ok=True, order_id=order_id, symbol=symbol,
                side=side, qty=qty, exchange=self.EXCHANGE_ID,
            )
        except Exception as e:
            logger.error("open_position %s: %s", symbol, e)
            return OrderResult(ok=False, symbol=symbol, error=str(e), exchange=self.EXCHANGE_ID)

    async def close_position(
        self, symbol: str, side: str, size: float
    ) -> OrderResult:
        close_side = "Sell" if side == "Buy" else "Buy"
        try:
            res = await self._run(
                self._session.place_order,
                category="linear",
                symbol=symbol,
                side=close_side,
                orderType="Market",
                qty=str(size),
                reduceOnly=True,
                timeInForce="IOC",
            )
            code = res.get("retCode", -1)
            return OrderResult(
                ok=code == 0,
                order_id=res.get("result", {}).get("orderId", ""),
                symbol=symbol,
                side=close_side,
                qty=size,
                error=res.get("retMsg", "") if code != 0 else "",
                exchange=self.EXCHANGE_ID,
            )
        except Exception as e:
            return OrderResult(ok=False, symbol=symbol, error=str(e), exchange=self.EXCHANGE_ID)

    async def set_trailing_stop(
        self,
        symbol: str,
        trailing_pct: float,
        active_price: Optional[float] = None,
    ) -> bool:
        """Bybit supporta trailing stop nativo."""
        try:
            params = dict(
                category="linear",
                symbol=symbol,
                trailingStop=str(round(trailing_pct, 4)),
                positionIdx=0,
            )
            if active_price:
                params["activePrice"] = str(round(active_price, 6))
            res = await self._run(self._session.set_trading_stop, **params)
            return res.get("retCode") == 0
        except Exception as e:
            logger.error("set_trailing_stop %s: %s", symbol, e)
            return False

    async def set_sl_tp(
        self,
        symbol: str,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
    ) -> bool:
        try:
            params = dict(
                category="linear",
                symbol=symbol,
                positionIdx=0,
            )
            if sl_price:
                params["stopLoss"] = str(round(sl_price, 6))
            if tp_price:
                params["takeProfit"] = str(round(tp_price, 6))
            res = await self._run(self._session.set_trading_stop, **params)
            return res.get("retCode") == 0
        except Exception as e:
            logger.error("set_sl_tp %s: %s", symbol, e)
            return False

    # ── INFO ──────────────────────────────────────────────────────────────────

    async def get_mark_price(self, symbol: str) -> float:
        try:
            res = await self._run(
                self._session.get_tickers,
                category="linear",
                symbol=symbol,
            )
            if res.get("retCode") != 0:
                return 0.0
            lst = res["result"].get("list", [])
            if not lst:
                return 0.0
            return self._sf(lst[0].get("markPrice"))
        except Exception as e:
            logger.error("get_mark_price %s: %s", symbol, e)
            return 0.0

    async def get_instrument_info(
        self, symbol: str
    ) -> Optional[InstrumentInfo]:
        try:
            res = await self._run(
                self._session.get_instruments_info,
                category="linear",
                symbol=symbol,
            )
            if res.get("retCode") != 0:
                return None
            lst = res["result"].get("list", [])
            if not lst:
                return None
            item = lst[0]
            lot  = item.get("lotSizeFilter", {})
            return InstrumentInfo(
                symbol=symbol,
                funding_interval_min=int(item.get("fundingInterval", 480)),
                upper_funding_rate=float(item.get("upperFundingRate", 0.00375)),
                lower_funding_rate=float(item.get("lowerFundingRate", -0.00375)),
                min_order_qty=self._sf(lot.get("minOrderQty")),
                qty_step=self._sf(lot.get("qtyStep")),
                exchange=self.EXCHANGE_ID,
            )
        except Exception as e:
            logger.error("get_instrument_info %s: %s", symbol, e)
            return None

    # ── DIAGNOSTICA ───────────────────────────────────────────────────────────

    async def test_connection(self) -> dict:
        results = {}

        # Public
        t0 = time.monotonic()
        try:
            res = await self._run(self._session.get_tickers, category="linear")
            lat = int((time.monotonic() - t0) * 1000)
            if res.get("retCode") == 0:
                results["public"] = {
                    "ok": True, "latency_ms": lat,
                    "symbols": len(res["result"]["list"]),
                }
            else:
                results["public"] = {"ok": False, "error": res.get("retMsg"), "latency_ms": lat}
        except Exception as e:
            results["public"] = {"ok": False, "error": str(e), "latency_ms": -1}

        # Auth (wallet)
        t0 = time.monotonic()
        try:
            res = await self._run(self._session.get_wallet_balance, accountType="UNIFIED")
            lat = int((time.monotonic() - t0) * 1000)
            if res.get("retCode") == 0:
                acc    = res["result"]["list"][0] if res["result"]["list"] else {}
                equity = self._sf(acc.get("totalEquity"))
                results["auth"] = {"ok": True, "latency_ms": lat, "equity": equity}
            else:
                results["auth"] = {"ok": False, "error": res.get("retMsg"), "latency_ms": lat}
        except Exception as e:
            results["auth"] = {"ok": False, "error": str(e), "latency_ms": -1}

        # Positions
        if results.get("auth", {}).get("ok"):
            t0 = time.monotonic()
            try:
                pos = await self.get_positions()
                lat = int((time.monotonic() - t0) * 1000)
                results["positions"] = {"ok": True, "latency_ms": lat, "open": len(pos)}
            except Exception as e:
                results["positions"] = {"ok": False, "error": str(e), "latency_ms": -1}
        else:
            results["positions"] = {"ok": False, "error": "Skipped (auth failed)"}

        return results

    async def get_open_interest(self, symbol: str) -> dict | None:
        """OI corrente e variazioni 5m/10m per un simbolo."""
        import requests as _req
        try:
            r = _req.get(
                "https://api.bybit.com/v5/market/open-interest",
                params={"category": "linear", "symbol": symbol,
                        "intervalTime": "5min", "limit": 3},
                timeout=10,
            )
            data = r.json()
            if data.get("retCode") != 0:
                return None
            items = data["result"]["list"]
            if len(items) < 3:
                return None
            curr  = float(items[0]["openInterest"])
            prev  = float(items[1]["openInterest"])
            prev2 = float(items[2]["openInterest"])
            return {
                "oi":         curr,
                "change_5m":  (curr - prev)  / prev  * 100 if prev  else 0,
                "change_10m": (curr - prev2) / prev2 * 100 if prev2 else 0,
            }
        except Exception as e:
            logger.error("get_open_interest %s: %s", symbol, e)
            return None
