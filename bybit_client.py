import os
"""
bybit_client.py — FundShot Bot
Client Bybit basato su pybit (HMAC automatico, retry, rate-limit).
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
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


def _make_session(testnet: bool = False, demo: bool = False) -> HTTP:
    api_key    = os.getenv("BYBIT_API_KEY", "")
    api_secret = os.getenv("BYBIT_API_SECRET", "")
    if api_key and api_secret:
        return HTTP(testnet=testnet, demo=demo, api_key=api_key, api_secret=api_secret)
    return HTTP(testnet=testnet)


def get_session(force_new: bool = False) -> HTTP:
    global _session
    if _session is None or force_new:
        _session = _make_session(testnet=os.getenv("TRADING_TESTNET","false").lower()=="true", demo=os.getenv("TRADING_DEMO","false").lower()=="true")
    return _session


def reload_session():
    """Ricrea la sessione dopo cambio credenziali."""
    global _session
    _session = _make_session(testnet=os.getenv("TRADING_TESTNET","false").lower()=="true", demo=os.getenv("TRADING_DEMO","false").lower()=="true")
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


# ── Storico funding (ultimi N cicli) ─────────────────────────────────────────
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


# ── Storico funding 7 giorni ──────────────────────────────────────────────────
async def get_funding_history_7d(symbol: str) -> list[dict]:
    """
    Restituisce tutti i cicli di funding degli ultimi 7 giorni per il simbolo.
    Usa startTime/endTime per coprire esattamente la finestra temporale.
    Gestisce automaticamente la paginazione (max 200 per chiamata).

    Ogni entry: { symbol, fundingRate (str), fundingRateTimestamp (str ms) }
    Ordine: dal più recente al meno recente.
    """
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - 7 * 24 * 3600 * 1000   # 7 giorni fa

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

            res = await _run(
                get_session().get_funding_rate_history,
                **kwargs,
            )
            if res.get("retCode") != 0:
                logger.error("get_funding_history_7d error: %s", res.get("retMsg"))
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
                "walletBalance": _sf(c.get("walletBalance")),
                "usdValue":      _sf(c.get("usdValue")),
                "unrealisedPnl": _sf(c.get("unrealisedPnl")),
            }
            for c in acc.get("coin", [])
            if _sf(c.get("walletBalance")) != 0
        ]
        return {
            "totalEquity":           _sf(acc.get("totalEquity")),
            "totalWalletBalance":    _sf(acc.get("totalWalletBalance")),
            "totalAvailableBalance": _sf(acc.get("totalAvailableBalance")),
            "totalPerpUPL":          _sf(acc.get("totalPerpUPL")),
            "totalMarginBalance":    _sf(acc.get("totalMarginBalance")),
            "totalInitialMargin":    _sf(acc.get("totalInitialMargin")),
            "coins": coins,
        }
    except Exception as e:
        logger.error("get_wallet_balance: %s", e)
        return None


# ── Posizioni aperte ──────────────────────────────────────────────────────────
def _sf(val, default: float = 0.0) -> float:
    """Safe float: converte stringhe vuote e None a default senza eccezione."""
    try:
        return float(val) if val not in (None, "", "—") else default
    except (TypeError, ValueError):
        return default


async def get_positions() -> list[dict]:
    """
    Restituisce tutte le posizioni aperte.
    Copre: linear (USDT + USDC) e inverse (coin-margined).
    Usa _sf() per resistere a campi stringa-vuota restituiti da Bybit.
    """
    positions = []
    queries = [
        {"category": "linear", "settleCoin": "USDT", "limit": 200},
        {"category": "linear", "settleCoin": "USDC", "limit": 200},
        {"category": "inverse", "limit": 200},
    ]
    for kwargs in queries:
        cat = kwargs["category"]
        try:
            res = await _run(get_session().get_positions, **kwargs)
            code = res.get("retCode")
            if code != 0:
                logger.warning(
                    "get_positions [%s/%s] retCode=%s msg=%s",
                    cat, kwargs.get("settleCoin", "—"),
                    code, res.get("retMsg"),
                )
                continue
            for p in res["result"]["list"]:
                size = _sf(p.get("size"))
                if size == 0:
                    continue
                position_im    = _sf(p.get("positionIM"))
                unrealised_pnl = _sf(p.get("unrealisedPnl"))
                pnl_pct = (unrealised_pnl / position_im * 100) if position_im else 0
                positions.append({
                    "symbol":         p.get("symbol", "?"),
                    "side":           p.get("side", "None"),
                    "size":           size,
                    "avgPrice":       _sf(p.get("avgPrice")),
                    "markPrice":      _sf(p.get("markPrice")),
                    "leverage":       p.get("leverage", "—"),
                    "unrealisedPnl":  unrealised_pnl,
                    "pnlPct":         pnl_pct,
                    "positionIM":     position_im,
                    "liqPrice":       _sf(p.get("liqPrice")),
                    "takeProfit":     _sf(p.get("takeProfit")),
                    "stopLoss":       _sf(p.get("stopLoss")),
                    "curRealisedPnl": _sf(p.get("curRealisedPnl")),
                    "positionStatus": p.get("positionStatus", "Normal"),
                    "category":       cat,
                })
        except Exception as e:
            logger.error("get_positions [%s/%s]: %s", cat, kwargs.get("settleCoin", "—"), e)
    return positions


async def test_positions_api() -> dict:
    """Diagnostica: ritorna retCode/retMsg per ogni query posizioni."""
    results = {}
    queries = [
        ("linear+USDT", {"category": "linear", "settleCoin": "USDT", "limit": 10}),
        ("linear+USDC", {"category": "linear", "settleCoin": "USDC", "limit": 10}),
        ("inverse",     {"category": "inverse", "limit": 10}),
    ]
    for label, kwargs in queries:
        try:
            res = await _run(get_session().get_positions, **kwargs)
            items = res.get("result", {}).get("list", [])
            nz = [p for p in items if _sf(p.get("size")) != 0]
            results[label] = {
                "retCode": res.get("retCode"),
                "retMsg":  res.get("retMsg", ""),
                "total":   len(items),
                "nonzero": len(nz),
            }
        except Exception as e:
            results[label] = {"error": str(e)}
    return results


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
            equity = _sf(acc.get("totalEquity"))
            results["auth"] = {"ok": True, "latency_ms": lat, "equity": equity}
        else:
            results["auth"] = {"ok": False, "error": res.get("retMsg"), "latency_ms": lat}
    except Exception as e:
        results["auth"] = {"ok": False, "error": str(e), "latency_ms": -1}

    # Test 3 — Positions (tutte le categorie)
    if results.get("auth", {}).get("ok"):
        t0 = time.monotonic()
        try:
            diag = await test_positions_api()
            lat  = int((time.monotonic() - t0) * 1000)
            total_nz = sum(d.get("nonzero", 0) for d in diag.values() if isinstance(d, dict))
            errors = [f"{lbl}: {d.get('retMsg',d.get('error','?'))}" for lbl,d in diag.items()
                      if isinstance(d,dict) and d.get("retCode",0) != 0]
            results["positions"] = {
                "ok":         not bool(errors) or total_nz > 0,
                "latency_ms": lat,
                "open":       total_nz,
                "detail":     diag,
                "errors":     errors,
            }
        except Exception as e:
            results["positions"] = {"ok": False, "error": str(e), "latency_ms": -1}
    else:
        results["positions"] = {"ok": False, "error": "Skipped (auth failed)", "latency_ms": -1}

    return results


async def close_position(symbol, side, size, category='linear'):
    close_side = 'Sell' if side == 'Buy' else 'Buy'
    try:
        res = await _run(get_session().place_order, category=category, symbol=symbol,
            side=close_side, orderType='Market', qty=str(size), reduceOnly=True, timeInForce='IOC')
        code = res.get('retCode', -1)
        return {'ok': code==0, 'symbol': symbol, 'side': close_side, 'size': size,
                'retCode': code, 'retMsg': res.get('retMsg',''), 'orderId': res.get('result',{}).get('orderId','')}
    except Exception as e:
        return {'ok': False, 'symbol': symbol, 'error': str(e)}

async def close_positions_by_mm(mm_threshold_pct=15.0):
    positions = await get_positions()
    results = []
    for p in positions:
        sym = p.get('symbol',''); side = p.get('side','Buy'); size = p.get('size',0.0)
        cat = p.get('category','linear'); liq = p.get('liqPrice',0.0); mark = p.get('markPrice',0.0)
        if mark <= 0 or liq <= 0: continue
        dist_pct = (mark - liq)/mark*100 if side=='Buy' else (liq - mark)/mark*100
        dist_pct = max(dist_pct, 0.0)
        if dist_pct <= mm_threshold_pct:
            res = await close_position(sym, side, size, cat)
            res['dist_pct_liq'] = round(dist_pct,2); res['trigger'] = 'mm'
            results.append(res)
    return results

async def close_positions_by_pnl(pnl_threshold_usdt):
    positions = await get_positions()
    if not positions: return []
    total_pnl = sum(p.get('unrealisedPnl',0.0) for p in positions)
    triggered = (pnl_threshold_usdt >= 0 and total_pnl >= pnl_threshold_usdt) or                 (pnl_threshold_usdt < 0  and total_pnl <= pnl_threshold_usdt)
    if not triggered:
        return [{'ok': False, 'msg': f'PnL {total_pnl:+.2f} non ha raggiunto soglia {pnl_threshold_usdt:+.2f}',
                 'total_pnl': total_pnl, 'trigger': 'pnl_not_reached'}]
    results = []
    for p in positions:
        res = await close_position(p.get('symbol',''), p.get('side','Buy'), p.get('size',0.0), p.get('category','linear'))
        res['unrealisedPnl'] = p.get('unrealisedPnl',0.0); res['total_pnl'] = round(total_pnl,4); res['trigger'] = 'pnl'
        results.append(res)
    return results
