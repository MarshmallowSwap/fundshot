"""
bybit_client.py — Funding King Bot
Client Bybit basato su pybit (HMAC automatico, retry, rate-limit).
"""

import asyncio
import logging
import os
import time
from typing import Optional

from pybit.unified_trading import HTTP

logger = logging.getLogger(__name__)

# ── Simboli esclusi dal meccanismo automatico intervallo ──────────────────────
EXCLUDED_AUTO_INTERVAL = {
    "BTCUSDT", "BTCUSDC", "BTCUSD",
    "ETHUSDT", "ETHUSDC", "ETHUSD",
    "ETHBTCUSDT", "ETHWUSDT",
}

# ── Sessione globale ───────────────────────────────────────────────────────────
_session: Optional[HTTP] = None


def _make_session(testnet: bool = False) -> HTTP:
    api_key    = os.getenv("BYBIT_API_KEY", "")
    api_secret = os.getenv("BYBIT_API_SECRET", "")
    if api_key and api_secret:
        return HTTP(testnet=testnet, api_key=api_key, api_secret=api_secret)
    return HTTP(testnet=testnet)


def get_session(force_new: bool = False) -> HTTP:
    global _session
    if _session is None or force_new:
        _session = _make_session()
    return _session


def reload_session():
    """Ricrea la sessione dopo cambio credenziali."""
    global _session
    _session = _make_session()
    logger.info("Sessione Bybit ricreata.")


async def _run(fn, *args, **kwargs):
    """Esegue una chiamata pybit sincrona in un thread separato."""
    return await asyncio.to_thread(fn, *args, **kwargs)


# ── Instruments info (cap funding per simbolo) ────────────────────────────────
async def get_instruments_info() -> dict[str, dict]:
    """
    Carica i parametri statici di tutti i simboli linear perpetual.
    Restituisce:
      { "BTCUSDT": { "fundingInterval": 480,
                     "upperFundingRate": 0.00375,
                     "lowerFundingRate": -0.00375 }, ... }
    Usa cursor per paginare (Bybit restituisce max 500 per volta).
    """
    result = {}
    cursor = ""
    while True:
        try:
            kwargs = {"category": "linear", "limit": 500}
            if cursor:
                kwargs["cursor"] = cursor
            res = await _run(get_session().get_instruments_info, **kwargs)
            if res.get("retCode") != 0:
                logger.error("get_instruments_info error: %s", res.get("retMsg"))
                break
            data = res["result"]
            for item in data.get("list", []):
                sym = item.get("symbol", "")
                if not sym.endswith("USDT"):
                    continue
                try:
                    result[sym] = {
                        "fundingInterval":   int(item.get("fundingInterval", 480)),  # minuti
                        "upperFundingRate":  float(item.get("upperFundingRate", 0.00375)),
                        "lowerFundingRate":  float(item.get("lowerFundingRate", -0.00375)),
                    }
                except (ValueError, TypeError):
                    pass
            cursor = data.get("nextPageCursor", "")
            if not cursor:
                break
        except Exception as e:
            logger.error("get_instruments_info: %s", e)
            break
    logger.info("Instruments info caricati: %d simboli", len(result))
    return result


# ── Funding rates (tickers) ───────────────────────────────────────────────────
async def get_funding_tickers() -> list[dict]:
    """
    Restituisce tutti i ticker lineari USDT con funding rate != 0.
    Campi utili: symbol, fundingRate, nextFundingTime, fundingIntervalHour,
                 lastPrice, price24hPcnt, prevPrice1h
    """
    try:
        res = await _run(get_session().get_tickers, category="linear")
        if res.get("retCode") != 0:
            logger.error("get_tickers error: %s", res.get("retMsg"))
            return []
        return [
            t for t in res["result"]["list"]
            if t.get("symbol", "").endswith("USDT")
            and float(t.get("fundingRate", 0)) != 0
        ]
    except Exception as e:
        logger.error("get_funding_tickers: %s", e)
        return []


# ── Storico funding ───────────────────────────────────────────────────────────
async def get_funding_history(symbol: str, limit: int = 8) -> list[dict]:
    """Storico funding rate per un simbolo (ultimi `limit` cicli)."""
    try:
        res = await _run(
            get_session().get_funding_rate_history,
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


# ── Saldo account ─────────────────────────────────────────────────────────────
async def get_wallet_balance() -> Optional[dict]:
    """Restituisce il saldo del conto Unified."""
    try:
        res = await _run(get_session().get_wallet_balance, accountType="UNIFIED")
        if res.get("retCode") != 0:
            logger.error("get_wallet_balance error: %s", res.get("retMsg"))
            return None
        accounts = res["result"]["list"]
        if not accounts:
            return None
        acc = accounts[0]
        coins = [
            {
                "coin":          c["coin"],
                "walletBalance": float(c.get("walletBalance", 0)),
                "usdValue":      float(c.get("usdValue", 0)),
                "unrealisedPnl": float(c.get("unrealisedPnl", 0)),
            }
            for c in acc.get("coin", [])
            if float(c.get("walletBalance", 0)) != 0
        ]
        return {
            "totalEquity":           float(acc.get("totalEquity", 0)),
            "totalWalletBalance":    float(acc.get("totalWalletBalance", 0)),
            "totalAvailableBalance": float(acc.get("totalAvailableBalance", 0)),
            "totalPerpUPL":          float(acc.get("totalPerpUPL", 0)),
            "totalMarginBalance":    float(acc.get("totalMarginBalance", 0)),
            "totalInitialMargin":    float(acc.get("totalInitialMargin", 0)),
            "coins": coins,
        }
    except Exception as e:
        logger.error("get_wallet_balance: %s", e)
        return None


# ── Posizioni aperte ──────────────────────────────────────────────────────────
async def get_positions() -> list[dict]:
    """Restituisce tutte le posizioni aperte (linear USDT perpetual)."""
    try:
        res = await _run(
            get_session().get_positions,
            category="linear",
            settleCoin="USDT",
        )
        if res.get("retCode") != 0:
            logger.error("get_positions error: %s", res.get("retMsg"))
            return []
        positions = []
        for p in res["result"]["list"]:
            size = float(p.get("size", 0))
            if size == 0:
                continue
            position_im   = float(p.get("positionIM", 0))
            unrealised_pnl = float(p.get("unrealisedPnl", 0))
            pnl_pct = (unrealised_pnl / position_im * 100) if position_im else 0
            positions.append({
                "symbol":         p["symbol"],
                "side":           p["side"],
                "size":           size,
                "avgPrice":       float(p.get("avgPrice", 0)),
                "markPrice":      float(p.get("markPrice", 0)),
                "leverage":       p.get("leverage", "—"),
                "unrealisedPnl":  unrealised_pnl,
                "pnlPct":         pnl_pct,
                "positionIM":     position_im,
                "liqPrice":       float(p.get("liqPrice", 0)),
                "takeProfit":     float(p.get("takeProfit", 0)),
                "stopLoss":       float(p.get("stopLoss", 0)),
                "curRealisedPnl": float(p.get("curRealisedPnl", 0)),
                "positionStatus": p.get("positionStatus", "Normal"),
            })
        return positions
    except Exception as e:
        logger.error("get_positions: %s", e)
        return []


# ── Test connessione ──────────────────────────────────────────────────────────
async def test_connection() -> dict:
    """Esegue 3 test di connessione e restituisce i risultati."""
    results = {}

    # Test 1 — Public API
    t0 = time.monotonic()
    try:
        res = await _run(get_session().get_tickers, category="linear")
        lat = int((time.monotonic() - t0) * 1000)
        if res.get("retCode") == 0:
            results["public"] = {"ok": True, "latency_ms": lat, "symbols": len(res["result"]["list"])}
        else:
            results["public"] = {"ok": False, "error": res.get("retMsg"), "latency_ms": lat}
    except Exception as e:
        results["public"] = {"ok": False, "error": str(e), "latency_ms": -1}

    # Test 2 — Authenticated (wallet)
    t0 = time.monotonic()
    try:
        res = await _run(get_session().get_wallet_balance, accountType="UNIFIED")
        lat = int((time.monotonic() - t0) * 1000)
        if res.get("retCode") == 0:
            acc    = res["result"]["list"][0] if res["result"]["list"] else {}
            equity = float(acc.get("totalEquity", 0))
            results["auth"] = {"ok": True, "latency_ms": lat, "equity": equity}
        else:
            results["auth"] = {"ok": False, "error": res.get("retMsg"), "latency_ms": lat}
    except Exception as e:
        results["auth"] = {"ok": False, "error": str(e), "latency_ms": -1}

    # Test 3 — Positions
    if results.get("auth", {}).get("ok"):
        t0 = time.monotonic()
        try:
            res = await _run(get_session().get_positions, category="linear", settleCoin="USDT")
            lat = int((time.monotonic() - t0) * 1000)
            if res.get("retCode") == 0:
                count = sum(1 for p in res["result"]["list"] if float(p.get("size", 0)) > 0)
                results["positions"] = {"ok": True, "latency_ms": lat, "open": count}
            else:
                results["positions"] = {"ok": False, "error": res.get("retMsg"), "latency_ms": lat}
        except Exception as e:
            results["positions"] = {"ok": False, "error": str(e), "latency_ms": -1}
    else:
        results["positions"] = {"ok": False, "error": "Skipped (auth failed)", "latency_ms": -1}

    return results
