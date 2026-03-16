"""
exchanges/okx.py — FundShot SaaS
Implementazione OKX di ExchangeClient.
Usa le REST API v5 di OKX (swap perpetuals).
OKX richiede anche una passphrase oltre ad API key/secret.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

import aiohttp

from .base import ExchangeClient
from .models import FundingTicker, Position, WalletBalance, InstrumentInfo, OrderResult

logger = logging.getLogger(__name__)

BASE_URL      = "https://www.okx.com"
BASE_URL_DEMO = "https://www.okx.com"   # OKX usa flag nel header per il paper trading


class OKXClient(ExchangeClient):

    EXCHANGE_ID = "okx"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        demo: bool = True,
        testnet: bool = False,
        passphrase: str = "",
        **kwargs,
    ):
        super().__init__(api_key, api_secret, demo, testnet)
        self._passphrase = passphrase
        self._base = BASE_URL

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        msg = timestamp + method.upper() + path + body
        return base64.b64encode(
            hmac.new(self.api_secret.encode(), msg.encode(), hashlib.sha256).digest()
        ).decode()

    def _auth_headers(self, method: str, path: str, body: str = "") -> dict:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        return {
            "OK-ACCESS-KEY":        self.api_key,
            "OK-ACCESS-SIGN":       self._sign(ts, method, path, body),
            "OK-ACCESS-TIMESTAMP":  ts,
            "OK-ACCESS-PASSPHRASE": self._passphrase,
            "x-simulated-trading":  "1" if self.demo else "0",
            "Content-Type":         "application/json",
        }

    async def _get(self, path: str, params: dict = None) -> dict:
        qs  = ("?" + urlencode(params)) if params else ""
        url = self._base + path + qs
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()

    async def _auth_get(self, path: str, params: dict = None) -> dict:
        qs      = ("?" + urlencode(params)) if params else ""
        full    = path + qs
        headers = self._auth_headers("GET", full)
        url     = self._base + full
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()

    async def _auth_post(self, path: str, body: dict = None) -> dict:
        import json
        body_str = json.dumps(body or {})
        headers  = self._auth_headers("POST", path, body_str)
        url      = self._base + path
        async with aiohttp.ClientSession() as s:
            async with s.post(url, data=body_str, headers=headers,
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
            # Tickers perpetual USDT-margined
            data = await self._get("/api/v5/market/tickers", {"instType": "SWAP"})
            if data.get("code") != "0":
                logger.error("get_funding_tickers OKX: %s", data.get("msg"))
                return []

            # Funding rates in bulk
            fr_data = await self._get("/api/v5/public/funding-rate-summary", {"instType": "SWAP"})
            fr_map  = {}
            if fr_data.get("code") == "0":
                for fr in fr_data.get("data", []):
                    fr_map[fr.get("instId")] = self._sf(fr.get("fundingRate"))

            tickers = []
            for t in data.get("data", []):
                inst_id = t.get("instId", "")
                # Solo perpetual USDT: formato BTC-USDT-SWAP
                if not inst_id.endswith("-USDT-SWAP"):
                    continue
                sym  = inst_id.replace("-USDT-SWAP", "USDT")   # → BTCUSDT
                rate = fr_map.get(inst_id, 0.0)
                if rate == 0:
                    continue
                tickers.append(FundingTicker(
                    symbol=sym,
                    funding_rate=rate,
                    next_funding_time=int(t.get("nextFundingTime", 0)) if t.get("nextFundingTime") else 0,
                    funding_interval_h=8.0,
                    last_price=self._sf(t.get("last")),
                    price_24h_pct=self._sf(t.get("chgUtc0")),
                    prev_price_1h=0.0,
                    exchange=self.EXCHANGE_ID,
                ))
            return tickers
        except Exception as e:
            logger.error("get_funding_tickers OKX: %s", e)
            return []

    async def get_funding_history(self, symbol: str, limit: int = 8) -> list[dict]:
        try:
            inst_id = symbol.replace("USDT", "-USDT-SWAP")
            data    = await self._get("/api/v5/public/funding-rate-history", {
                "instId": inst_id,
                "limit":  limit,
            })
            return [
                {
                    "symbol":      symbol,
                    "fundingRate": d.get("fundingRate"),
                    "fundingTime": int(d.get("fundingTime", 0)),
                }
                for d in data.get("data", [])
            ]
        except Exception as e:
            logger.error("get_funding_history OKX %s: %s", symbol, e)
            return []

    async def get_funding_history_7d(self, symbol: str) -> list[dict]:
        now_ms   = int(time.time() * 1000)
        start_ms = now_ms - 7 * 24 * 3600 * 1000
        inst_id  = symbol.replace("USDT", "-USDT-SWAP")
        try:
            data = await self._get("/api/v5/public/funding-rate-history", {
                "instId": inst_id,
                "after":  start_ms,
                "limit":  "168",
            })
            return [
                {
                    "symbol":      symbol,
                    "fundingRate": d.get("fundingRate"),
                    "fundingTime": int(d.get("fundingTime", 0)),
                }
                for d in data.get("data", [])
            ]
        except Exception as e:
            logger.error("get_funding_history_7d OKX %s: %s", symbol, e)
            return []

    async def get_instruments_info(self, symbol: str = "") -> list[InstrumentInfo]:
        try:
            params  = {"instType": "SWAP"}
            if symbol:
                params["instId"] = symbol.replace("USDT", "-USDT-SWAP")
            data = await self._get("/api/v5/public/instruments", params)
            result = []
            for s in data.get("data", []):
                if not s.get("instId", "").endswith("-USDT-SWAP"):
                    continue
                sym = s["instId"].replace("-USDT-SWAP", "USDT")
                result.append(InstrumentInfo(
                    symbol=sym,
                    max_leverage=int(self._sf(s.get("lever", 20))),
                    min_qty=self._sf(s.get("minSz", 0.001)),
                    qty_step=self._sf(s.get("lotSz", 0.001)),
                    tick_size=self._sf(s.get("tickSz", 0.0001)),
                    max_funding_rate=0.03,
                ))
            return result
        except Exception as e:
            logger.error("get_instruments_info OKX: %s", e)
            return []

    # ── ACCOUNT ───────────────────────────────────────────────────────────────

    async def get_wallet_balance(self) -> Optional[WalletBalance]:
        try:
            data = await self._auth_get("/api/v5/account/balance")
            if data.get("code") != "0":
                logger.error("get_wallet_balance OKX code=%s msg=%s", data.get("code"), data.get("msg"))
                return None
            acc     = data.get("data", [{}])[0]
            details = acc.get("details", [])
            # Equity totale account (tutti i coin valorizzati in USD)
            total_eq  = self._sf(acc.get("totalEq"))
            total_avl = sum(self._sf(d.get("availEq")) for d in details)
            total_upl = sum(self._sf(d.get("upl"))     for d in details)
            # Coins con saldo
            coins = [
                {"coin": d.get("ccy"), "walletBalance": self._sf(d.get("eq")),
                 "usdValue": self._sf(d.get("eq")), "unrealisedPnl": self._sf(d.get("upl"))}
                for d in details if self._sf(d.get("eq")) != 0
            ]
            return WalletBalance(
                total_equity=total_eq,
                total_wallet_balance=total_eq,
                total_available_balance=total_avl,
                total_perp_upl=total_upl,
                total_margin_balance=total_eq - total_avl,
                coins=coins,
                exchange=self.EXCHANGE_ID,
            )
        except Exception as e:
            logger.error("get_wallet_balance OKX: %s", e)
            return None

    async def get_positions(self) -> list[Position]:
        try:
            data = await self._auth_get("/api/v5/account/positions", {"instType": "SWAP"})
            if data.get("code") != "0":
                return []
            positions = []
            for p in data.get("data", []):
                size = self._sf(p.get("pos"))
                if size == 0:
                    continue
                side = "Buy" if p.get("posSide") == "long" else "Sell"
                inst = p.get("instId", "").replace("-USDT-SWAP", "USDT")
                positions.append(Position(
                    symbol=inst,
                    side=side,
                    size=abs(size),
                    entry_price=self._sf(p.get("avgPx")),
                    mark_price=self._sf(p.get("markPx")),
                    unrealised_pnl=self._sf(p.get("upl")),
                    leverage=int(self._sf(p.get("lever", 1))),
                    liq_price=self._sf(p.get("liqPx")),
                    exchange=self.EXCHANGE_ID,
                ))
            return positions
        except Exception as e:
            logger.error("get_positions OKX: %s", e)
            return []

    # ── TRADING ───────────────────────────────────────────────────────────────

    async def open_position(self, symbol: str, side: str, qty: float,
                             leverage: int = 5, tp_price: float = 0.0,
                             sl_price: float = 0.0) -> OrderResult:
        try:
            inst_id   = symbol.replace("USDT", "-USDT-SWAP")
            okx_side  = "buy" if side == "Buy" else "sell"
            pos_side  = "long" if side == "Buy" else "short"

            # Imposta leva
            await self._auth_post("/api/v5/account/set-leverage", {
                "instId": inst_id, "lever": str(leverage), "mgnMode": "cross",
            })

            body = {
                "instId":  inst_id,
                "tdMode":  "cross",
                "side":    okx_side,
                "posSide": pos_side,
                "ordType": "market",
                "sz":      str(qty),
            }
            if tp_price:
                body["tpTriggerPx"] = str(tp_price)
                body["tpOrdPx"]     = "-1"
            if sl_price:
                body["slTriggerPx"] = str(sl_price)
                body["slOrdPx"]     = "-1"

            res = await self._auth_post("/api/v5/trade/order", body)
            if res.get("code") != "0":
                return OrderResult(ok=False, order_id="", error=res.get("msg", str(res)))
            order_id = res["data"][0].get("ordId", "")
            return OrderResult(ok=True, order_id=order_id)
        except Exception as e:
            return OrderResult(ok=False, order_id="", error=str(e))

    async def close_position(self, symbol: str, side: str, qty: float) -> bool:
        try:
            inst_id  = symbol.replace("USDT", "-USDT-SWAP")
            close_s  = "sell" if side == "Buy" else "buy"
            pos_side = "long" if side == "Buy" else "short"
            res = await self._auth_post("/api/v5/trade/order", {
                "instId":   inst_id,
                "tdMode":   "cross",
                "side":     close_s,
                "posSide":  pos_side,
                "ordType":  "market",
                "sz":       str(qty),
                "reduceOnly": "true",
            })
            return res.get("code") == "0"
        except Exception as e:
            logger.error("close_position OKX %s: %s", symbol, e)
            return False

    async def set_trailing_stop(self, symbol: str, side: str,
                                 trailing_dist: float, active_price: float) -> bool:
        # OKX non ha trailing stop nativo semplice — gestione manuale
        logger.info("set_trailing_stop OKX: gestione manuale attiva per %s", symbol)
        return False

    async def set_sl_tp(self, symbol: str, side: str,
                         sl_price: float = 0.0, tp_price: float = 0.0) -> bool:
        try:
            inst_id  = symbol.replace("USDT", "-USDT-SWAP")
            pos_side = "long" if side == "Buy" else "short"
            algo_ords = []
            if tp_price:
                algo_ords.append({
                    "instId": inst_id, "tdMode": "cross", "posSide": pos_side,
                    "ordType": "oco", "tpTriggerPx": str(tp_price), "tpOrdPx": "-1",
                    "slTriggerPx": str(sl_price) if sl_price else "",
                })
            if algo_ords:
                res = await self._auth_post("/api/v5/trade/order-algo", algo_ords[0])
                return res.get("code") == "0"
            return True
        except Exception as e:
            logger.error("set_sl_tp OKX %s: %s", symbol, e)
            return False

    # ── INFO ──────────────────────────────────────────────────────────────────

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        try:
            inst_id = symbol.replace("USDT", "-USDT-SWAP")
            data    = await self._get("/api/v5/public/mark-price",
                                      {"instType": "SWAP", "instId": inst_id})
            return self._sf(data["data"][0].get("markPx")) or None
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
            data = await self._get("/api/v5/public/time")
            lat  = int((time.monotonic() - t0) * 1000)
            results["public"] = {"ok": data.get("code") == "0", "latency_ms": lat}
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
                    results["auth"] = {"ok": False, "error": "Auth failed or no USDT balance", "latency_ms": lat}
            except Exception as e:
                results["auth"] = {"ok": False, "error": str(e), "latency_ms": -1}
        else:
            results["auth"] = {"ok": False, "error": "No API keys configured"}

        return results
