import time
import hmac
import hashlib
import logging
import aiohttp
from urllib.parse import urlencode


class BybitClient:
    BASE_URL = "https://api.bybit.com"

    def __init__(self, api_key: str | None, api_secret: str | None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.has_keys = bool(api_key and api_secret)

    def _sign(self, timestamp: str, recv_window: str, query_string: str) -> str:
        """Firma HMAC SHA256 corretta per Bybit v5 GET requests."""
        sign_payload = timestamp + self.api_key + recv_window + query_string
        return hmac.new(
            self.api_secret.encode(),
            sign_payload.encode(),
            hashlib.sha256,
        ).hexdigest()

    async def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        auth: bool = False,
    ):
        url = self.BASE_URL + path
        params = params or {}
        headers = {"Content-Type": "application/json"}

        if auth:
            if not self.has_keys:
                logging.warning("Richiesta privata Bybit senza API key configurate.")
                return None

            timestamp = str(int(time.time() * 1000))
            recv_window = "5000"

            if method == "GET":
                query_string = urlencode(params)
            else:
                query_string = ""

            signature = self._sign(timestamp, recv_window, query_string)
            headers.update({
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": recv_window,
                "X-BAPI-SIGN": signature,
            })

        try:
            async with aiohttp.ClientSession() as session:
                if method == "GET":
                    async with session.get(
                        url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=8)
                    ) as resp:
                        return await resp.json()
                else:
                    async with session.post(
                        url, headers=headers, json=params, timeout=aiohttp.ClientTimeout(total=8)
                    ) as resp:
                        return await resp.json()
        except aiohttp.ClientConnectorError:
            logging.error(f"Bybit non raggiungibile: {path}")
            return None
        except Exception as e:
            logging.error(f"Errore richiesta Bybit {method} {path}: {e}")
            return None

    # ─── API PUBBLICA ───────────────────────────────────────────────

    async def get_funding_rates(self) -> list:
        """Ritorna tutti i ticker linear USDT con fundingRate e fundingIntervalHour."""
        res = await self._request("GET", "/v5/market/tickers", {"category": "linear"})
        if not res or res.get("retCode") != 0:
            logging.error(f"Errore get_funding_rates: {res}")
            return []
        tickers = res["result"]["list"]
        # Filtra solo USDT perpetual con funding rate valido
        result = []
        for t in tickers:
            symbol = t.get("symbol", "")
            rate_raw = t.get("fundingRate")
            interval = t.get("fundingIntervalHour", "8")
            if not symbol.endswith("USDT"):
                continue
            if rate_raw is None:
                continue
            try:
                rate = float(rate_raw) * 100
            except Exception:
                continue
            result.append({
                "symbol": symbol,
                "rate": rate,
                "interval": str(interval),
            })
        return result

    # ─── API PRIVATE ────────────────────────────────────────────────

    async def get_wallet_balance(self) -> dict | None:
        """Ritorna saldo wallet Unified con campi chiave."""
        res = await self._request(
            "GET",
            "/v5/account/wallet-balance",
            params={"accountType": "UNIFIED"},
            auth=True,
        )
        if not res or res.get("retCode") != 0:
            logging.error(f"Errore get_wallet_balance: {res}")
            return None

        account = res["result"]["list"][0]
        coins = {}
        for coin in account.get("coin", []):
            name = coin.get("coin", "")
            balance = float(coin.get("walletBalance") or 0)
            if balance > 0:
                coins[name] = balance

        return {
            "totalEquity": float(account.get("totalEquity") or 0),
            "totalWalletBalance": float(account.get("totalWalletBalance") or 0),
            "totalAvailableBalance": float(account.get("totalAvailableBalance") or 0),
            "totalMarginBalance": float(account.get("totalMarginBalance") or 0),
            "totalInitialMargin": float(account.get("totalInitialMargin") or 0),
            "totalPerpUPL": float(account.get("totalPerpUPL") or 0),
            "coins": coins,
        }

    async def get_positions(self) -> list:
        """Ritorna tutte le posizioni aperte USDT linear."""
        res = await self._request(
            "GET",
            "/v5/position/list",
            params={"category": "linear", "settleCoin": "USDT", "limit": 200},
            auth=True,
        )
        if not res or res.get("retCode") != 0:
            logging.error(f"Errore get_positions: {res}")
            return []

        positions = []
        for p in res["result"]["list"]:
            size = float(p.get("size") or 0)
            if size == 0:
                continue

            position_im = float(p.get("positionIM") or 0)
            unrealised_pnl = float(p.get("unrealisedPnl") or 0)
            pnl_pct = (unrealised_pnl / position_im * 100) if position_im > 0 else 0.0

            tp = p.get("takeProfit", "0")
            sl = p.get("stopLoss", "0")

            positions.append({
                "symbol": p.get("symbol", ""),
                "side": p.get("side", ""),          # "Buy" o "Sell"
                "size": size,
                "avgPrice": float(p.get("avgPrice") or 0),
                "markPrice": float(p.get("markPrice") or 0),
                "leverage": p.get("leverage", "1"),
                "unrealisedPnl": unrealised_pnl,
                "pnlPct": pnl_pct,
                "positionIM": position_im,
                "liqPrice": p.get("liqPrice", ""),
                "takeProfit": tp if tp != "0" else "",
                "stopLoss": sl if sl != "0" else "",
                "positionStatus": p.get("positionStatus", "Normal"),
            })
        return positions

    async def ping_public(self) -> dict:
        """Test connessione API pubblica. Ritorna latenza e conteggio simboli."""
        import time as t
        start = t.time()
        res = await self._request("GET", "/v5/market/tickers", {"category": "linear"})
        latency = int((t.time() - start) * 1000)
        if res and res.get("retCode") == 0:
            count = len(res["result"]["list"])
            example = None
            for item in res["result"]["list"]:
                if item.get("symbol") == "BTCUSDT":
                    rate = float(item.get("fundingRate", 0)) * 100
                    interval = item.get("fundingIntervalHour", "8")
                    example = f"BTCUSDT → {rate:+.4f}%  ({interval}H)"
                    break
            return {"ok": True, "latency": latency, "count": count, "example": example}
        return {"ok": False, "latency": latency, "error": str(res)}

    async def ping_auth(self) -> dict:
        """Test connessione API autenticata."""
        import time as t
        start = t.time()
        res = await self._request(
            "GET",
            "/v5/account/wallet-balance",
            params={"accountType": "UNIFIED"},
            auth=True,
        )
        latency = int((t.time() - start) * 1000)
        if res and res.get("retCode") == 0:
            coins = res["result"]["list"][0].get("coin", [])
            usdt = next((c for c in coins if c.get("coin") == "USDT"), None)
            usdt_balance = float(usdt.get("walletBalance") or 0) if usdt else 0.0
            return {"ok": True, "latency": latency, "retCode": 0, "usdt": usdt_balance}
        error_msg = res.get("retMsg", "Errore sconosciuto") if res else "Timeout / non raggiungibile"
        ret_code = res.get("retCode") if res else None
        return {"ok": False, "latency": latency, "error": error_msg, "retCode": ret_code}

    async def ping_positions(self) -> dict:
        """Test connessione API posizioni."""
        import time as t
        start = t.time()
        res = await self._request(
            "GET",
            "/v5/position/list",
            params={"category": "linear", "settleCoin": "USDT", "limit": 1},
            auth=True,
        )
        latency = int((t.time() - start) * 1000)
        if res and res.get("retCode") == 0:
            count = len([p for p in res["result"]["list"] if float(p.get("size") or 0) > 0])
            return {"ok": True, "latency": latency, "count": count}
        error_msg = res.get("retMsg", "Errore") if res else "Timeout"
        return {"ok": False, "latency": latency, "error": error_msg}
