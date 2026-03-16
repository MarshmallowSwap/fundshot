"""
exchanges/binance.py — FundShot SaaS
Implementazione Binance Futures di ExchangeClient.
Usa le REST API pubbliche/private di Binance FAPI (futures).
"""

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Optional
from urllib.parse import urlencode

import aiohttp

from .base import ExchangeClient
from .models import FundingTicker, Position, WalletBalance, InstrumentInfo, OrderResult

logger = logging.getLogger(__name__)

BASE_URL      = "https://fapi.binance.com"
BASE_URL_TEST = "https://testnet.binancefuture.com"


class BinanceClient(ExchangeClient):

    EXCHANGE_ID = "binance"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        demo: bool = True,
        testnet: bool = False,
        **kwargs,
    ):
        super().__init__(api_key, api_secret, demo, testnet)
        self._base = BASE_URL_TEST if (testnet or demo) else BASE_URL

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    async def _get(self, path: str, params: dict = None) -> dict:
        url = self._base + path
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params or {}, timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()

    async def _signed_get(self, path: str, params: dict = None) -> dict:
        params = params or {}
        params["timestamp"] = int(time.time() * 1000)
        query   = urlencode(params)
        sig     = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        headers = {"X-MBX-APIKEY": self.api_key}
        url = self._base + path
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, headers=headers,
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()

    async def _signed_post(self, path: str, params: dict = None) -> dict:
        params = params or {}
        params["timestamp"] = int(time.time() * 1000)
        query   = urlencode(params)
        sig     = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        headers = {"X-MBX-APIKEY": self.api_key}
        url = self._base + path
        async with aiohttp.ClientSession() as s:
            async with s.post(url, params=params, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()

    @staticmethod
    def _sf(v, default=0.0) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    # ── MONITORING ────────────────────────────────────────────────────────────

    async def get_funding_tickers(self) -> list[FundingTicker]:
        try:
            # Premium index: contiene funding rate corrente + next funding time
            data = await self._get("/fapi/v1/premiumIndex")
            if not isinstance(data, list):
                logger.error("get_funding_tickers Binance: risposta inattesa %s", data)
                return []
            tickers = []
            for t in data:
                sym = t.get("symbol", "")
                if not sym.endswith("USDT"):
                    continue
                rate = self._sf(t.get("lastFundingRate"))
                if rate == 0:
                    continue
                tickers.append(FundingTicker(
                    symbol=sym,
                    funding_rate=rate,
                    next_funding_time=int(t.get("nextFundingTime", 0)),
                    funding_interval_h=8.0,   # Binance usa cicli fissi da 8h
                    last_price=self._sf(t.get("markPrice")),
                    price_24h_pct=0.0,        # non disponibile in premiumIndex
                    prev_price_1h=0.0,
                    exchange=self.EXCHANGE_ID,
                ))
            return tickers
        except Exception as e:
            logger.error("get_funding_tickers Binance: %s", e)
            return []

    async def get_funding_history(self, symbol: str, limit: int = 8) -> list[dict]:
        try:
            data = await self._get("/fapi/v1/fundingRate", {
                "symbol": symbol,
                "limit":  limit,
            })
            return [
                {
                    "symbol":      d.get("symbol"),
                    "fundingRate": d.get("fundingRate"),
                    "fundingTime": d.get("fundingTime"),
                }
                for d in (data if isinstance(data, list) else [])
            ]
        except Exception as e:
            logger.error("get_funding_history Binance %s: %s", symbol, e)
            return []

    async def get_funding_history_7d(self, symbol: str) -> list[dict]:
        now_ms   = int(time.time() * 1000)
        start_ms = now_ms - 7 * 24 * 3600 * 1000
        try:
            data = await self._get("/fapi/v1/fundingRate", {
                "symbol":    symbol,
                "startTime": start_ms,
                "endTime":   now_ms,
                "limit":     1000,
            })
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error("get_funding_history_7d Binance %s: %s", symbol, e)
            return []

    async def get_instruments_info(self, symbol: str = "") -> list[InstrumentInfo]:
        try:
            params = {"symbol": symbol} if symbol else {}
            data   = await self._get("/fapi/v1/exchangeInfo", params)
            result = []
            for s in data.get("symbols", []):
                if s.get("contractType") != "PERPETUAL":
                    continue
                result.append(InstrumentInfo(
                    symbol=s.get("symbol", ""),
                    max_leverage=20,
                    min_qty=self._sf(s.get("filters", [{}])[0].get("minQty", 0.001)),
                    qty_step=0.001,
                    tick_size=0.0001,
                    max_funding_rate=0.03,
                ))
            return result
        except Exception as e:
            logger.error("get_instruments_info Binance: %s", e)
            return []

    # ── ACCOUNT ───────────────────────────────────────────────────────────────

    async def get_wallet_balance(self) -> Optional[WalletBalance]:
        try:
            data = await self._signed_get("/fapi/v2/account")
            if "totalWalletBalance" not in data:
                logger.error("get_wallet_balance Binance unexpected response: %s", str(data)[:200])
                return None
            # Coins con saldo
            assets = data.get("assets", [])
            coins = [
                {
                    "coin":             a.get("asset"),
                    "walletBalance":    self._sf(a.get("walletBalance")),
                    "marginBalance":    self._sf(a.get("marginBalance")),
                    "unrealisedPnl":    self._sf(a.get("unrealizedProfit")),
                    "availableBalance": self._sf(a.get("availableBalance") or a.get("crossWalletBalance")),
                    "usdValue":         self._sf(a.get("marginBalance")),
                }
                for a in assets if self._sf(a.get("walletBalance")) != 0
            ]
            return WalletBalance(
                total_equity=self._sf(data.get("totalMarginBalance")),
                total_wallet_balance=self._sf(data.get("totalWalletBalance")),
                total_available_balance=self._sf(data.get("availableBalance")),
                total_perp_upl=self._sf(data.get("totalUnrealizedProfit")),
                total_margin_balance=self._sf(data.get("totalMaintMargin")),
                coins=coins,
                exchange=self.EXCHANGE_ID,
            )
        except Exception as e:
            logger.error("get_wallet_balance Binance: %s", e)
            return None

    async def get_positions(self) -> list[Position]:
        try:
            data = await self._signed_get("/fapi/v2/positionRisk")
            if not isinstance(data, list):
                return []
            positions = []
            for p in data:
                size = self._sf(p.get("positionAmt"))
                if size == 0:
                    continue
                side = "Buy" if size > 0 else "Sell"
                positions.append(Position(
                    symbol=p.get("symbol", ""),
                    side=side,
                    size=abs(size),
                    entry_price=self._sf(p.get("entryPrice")),
                    mark_price=self._sf(p.get("markPrice")),
                    unrealised_pnl=self._sf(p.get("unRealizedProfit")),
                    leverage=int(self._sf(p.get("leverage", 1))),
                    liq_price=self._sf(p.get("liquidationPrice")),
                    exchange=self.EXCHANGE_ID,
                ))
            return positions
        except Exception as e:
            logger.error("get_positions Binance: %s", e)
            return []

    # ── TRADING ───────────────────────────────────────────────────────────────

    async def open_position(self, symbol: str, side: str, qty: float,
                             leverage: int = 5, tp_price: float = 0.0,
                             sl_price: float = 0.0) -> OrderResult:
        try:
            # Imposta leva
            await self._signed_post("/fapi/v1/leverage", {
                "symbol": symbol, "leverage": leverage,
            })
            # Apri ordine market
            params = {
                "symbol":   symbol,
                "side":     "BUY" if side == "Buy" else "SELL",
                "type":     "MARKET",
                "quantity": qty,
            }
            res = await self._signed_post("/fapi/v1/order", params)
            if "orderId" not in res:
                return OrderResult(ok=False, order_id="", error=str(res.get("msg", res)))
            return OrderResult(ok=True, order_id=str(res["orderId"]))
        except Exception as e:
            return OrderResult(ok=False, order_id="", error=str(e))

    async def close_position(self, symbol: str, side: str, qty: float) -> bool:
        try:
            close_side = "SELL" if side == "Buy" else "BUY"
            res = await self._signed_post("/fapi/v1/order", {
                "symbol":           symbol,
                "side":             close_side,
                "type":             "MARKET",
                "quantity":         qty,
                "reduceOnly":       "true",
            })
            return "orderId" in res
        except Exception as e:
            logger.error("close_position Binance %s: %s", symbol, e)
            return False

    async def set_trailing_stop(self, symbol: str, side: str,
                                 trailing_dist: float, active_price: float) -> bool:
        # Binance supporta trailing stop nativamente
        try:
            close_side = "SELL" if side == "Buy" else "BUY"
            callback   = round(trailing_dist / active_price * 100, 1)
            callback   = max(0.1, min(callback, 5.0))
            res = await self._signed_post("/fapi/v1/order", {
                "symbol":            symbol,
                "side":              close_side,
                "type":              "TRAILING_STOP_MARKET",
                "callbackRate":      callback,
                "activationPrice":   active_price,
                "reduceOnly":        "true",
                "quantity":          0,   # Binance usa closePosition=true per tutto
                "closePosition":     "true",
            })
            return "orderId" in res
        except Exception as e:
            logger.warning("set_trailing_stop Binance %s: %s", symbol, e)
            return False

    async def set_sl_tp(self, symbol: str, side: str,
                         sl_price: float = 0.0, tp_price: float = 0.0) -> bool:
        try:
            close_side = "SELL" if side == "Buy" else "BUY"
            ok = True
            if tp_price > 0:
                res = await self._signed_post("/fapi/v1/order", {
                    "symbol": symbol, "side": close_side,
                    "type": "TAKE_PROFIT_MARKET", "stopPrice": tp_price,
                    "closePosition": "true",
                })
                ok = ok and ("orderId" in res)
            if sl_price > 0:
                res = await self._signed_post("/fapi/v1/order", {
                    "symbol": symbol, "side": close_side,
                    "type": "STOP_MARKET", "stopPrice": sl_price,
                    "closePosition": "true",
                })
                ok = ok and ("orderId" in res)
            return ok
        except Exception as e:
            logger.error("set_sl_tp Binance %s: %s", symbol, e)
            return False

    # ── INFO ──────────────────────────────────────────────────────────────────

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        try:
            data = await self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
            return self._sf(data.get("markPrice")) or None
        except Exception:
            return None

    async def get_instrument_info(self, symbol: str) -> Optional[InstrumentInfo]:
        infos = await self.get_instruments_info(symbol)
        return infos[0] if infos else None

    # ── DIAGNOSTICA ───────────────────────────────────────────────────────────

    async def test_connection(self) -> dict:
        results = {}

        # Public
        t0 = time.monotonic()
        try:
            data = await self._get("/fapi/v1/ping")
            lat  = int((time.monotonic() - t0) * 1000)
            results["public"] = {"ok": True, "latency_ms": lat}
        except Exception as e:
            results["public"] = {"ok": False, "error": str(e), "latency_ms": -1}

        # Auth
        if self.api_key and self.api_secret:
            t0 = time.monotonic()
            try:
                wb  = await self.get_wallet_balance()
                lat = int((time.monotonic() - t0) * 1000)
                if wb:
                    results["auth"] = {"ok": True, "latency_ms": lat, "equity": wb.total_equity}
                else:
                    results["auth"] = {"ok": False, "error": "No balance data", "latency_ms": lat}
            except Exception as e:
                results["auth"] = {"ok": False, "error": str(e), "latency_ms": -1}
        else:
            results["auth"] = {"ok": False, "error": "No API keys configured"}

        return results
