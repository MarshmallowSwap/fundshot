"""
oi_monitor.py — Monitoraggio OI spike su tutti i simboli perpetual USDT.
Alert indipendente dal funding rate.

Soglia default: spike >= 3% in 5min (o <= -3% per crollo)
"""
import logging
import time
import requests

logger = logging.getLogger(__name__)

OI_SPIKE_THRESHOLD = 3.0
OI_DROP_THRESHOLD  = -3.0
_last_oi_alert: dict[str, float] = {}
OI_COOLDOWN_SEC = 300


def _fetch_oi(symbol: str) -> dict | None:
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/open-interest",
            params={"category": "linear", "symbol": symbol,
                    "intervalTime": "5min", "limit": 3},
            timeout=8
        )
        data = r.json()
        if data.get("retCode") == 0:
            items = data["result"]["list"]
            if len(items) < 2:
                return None
            curr = float(items[0]["openInterest"])
            prev = float(items[1]["openInterest"])
            chg  = (curr - prev) / prev * 100 if prev else 0
            return {"oi": curr, "change_5m": chg}
    except Exception as e:
        logger.debug("fetch_oi %s: %s", symbol, e)
    return None


def _fetch_funding(symbol: str) -> float | None:
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "linear", "symbol": symbol},
            timeout=5
        )
        data = r.json()
        if data.get("retCode") == 0:
            items = data["result"]["list"]
            if items:
                return float(items[0].get("fundingRate", 0)) * 100
    except Exception:
        pass
    return None


def _get_suggestion(oi_chg: float, funding_pct: float | None) -> str:
    """
    Genera azione consigliata basata su OI + funding.

    OI ▲ = nuove posizioni entrano → trend in accelerazione
      + funding negativo → short pagano → APRI LONG
      + funding positivo → long pagano  → APRI SHORT
      + funding neutro   → spike speculativo

    OI ▼ = posizioni si chiudono → trend in esaurimento
      → CHIUDI posizioni aperte
    """
    if funding_pct is None:
        if oi_chg >= OI_SPIKE_THRESHOLD:
            return "📊 OI in forte crescita — funding non disponibile, monitora direzione"
        else:
            return "⚠️ OI in forte calo — riduci esposizione"

    abs_f = abs(funding_pct)
    strength = "forte" if abs_f >= 0.5 else "moderato" if abs_f >= 0.1 else "debole"

    if oi_chg >= OI_SPIKE_THRESHOLD:
        if funding_pct < -0.01:
            return (
                "🟢 *APRI LONG*\n"
                "Short pagano funding (" + f"{funding_pct:+.4f}%" + "), nuove posizioni long entrano\n"
                "Segnale: " + strength
            )
        elif funding_pct > 0.01:
            return (
                "🔴 *APRI SHORT*\n"
                "Long pagano funding (" + f"{funding_pct:+.4f}%" + "), nuove posizioni short entrano\n"
                "Segnale: " + strength
            )
        else:
            return (
                "⚪ *ATTENZIONE* — spike speculativo\n"
                "Funding neutro (" + f"{funding_pct:+.4f}%" + "), nessuna direzione chiara\n"
                "Evita nuove posizioni"
            )
    else:
        if funding_pct < -0.01:
            return (
                "🟡 *CHIUDI LONG* (se aperta)\n"
                "Momentum in esaurimento — long escono dal mercato\n"
                "Funding ancora negativo (" + f"{funding_pct:+.4f}%" + ") ma OI cala"
            )
        elif funding_pct > 0.01:
            return (
                "🟡 *CHIUDI SHORT* (se aperta)\n"
                "Momentum in esaurimento — short escono dal mercato\n"
                "Funding ancora positivo (" + f"{funding_pct:+.4f}%" + ") ma OI cala"
            )
        else:
            return (
                "⚠️ *ESCI DA POSIZIONI*\n"
                "Mercato si svuota — funding neutro (" + f"{funding_pct:+.4f}%" + "), OI in calo\n"
                "Volatilita in diminuzione"
            )


def format_oi_spike_alert(symbol: str, oi_chg: float, funding_pct: float | None) -> str:
    arrow    = "▲" if oi_chg > 0 else "▼"
    emoji    = "⚡" if oi_chg >= OI_SPIKE_THRESHOLD else "📉"
    kind     = "SPIKE" if oi_chg >= OI_SPIKE_THRESHOLD else "CROLLO"
    f_str    = f"{funding_pct:+.4f}%" if funding_pct is not None else "n/d"
    suggestion = _get_suggestion(oi_chg, funding_pct)

    return (
        f"{emoji} *OI {kind}*\n"
        f"*{symbol}*\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f4ca OI 5m:    `{arrow} {oi_chg:+.2f}%`\n"
        f"\U0001f4b8 Funding:  `{f_str}`\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"{suggestion}"
    )


def check_oi_spikes(tickers: list) -> list[tuple[str, str, float]]:
    alerts = []
    now = time.monotonic()

    for ticker in tickers:
        symbol = ticker.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        if now - _last_oi_alert.get(symbol, 0) < OI_COOLDOWN_SEC:
            continue

        oi_data = _fetch_oi(symbol)
        if not oi_data:
            continue

        chg = oi_data["change_5m"]
        if chg >= OI_SPIKE_THRESHOLD or chg <= OI_DROP_THRESHOLD:
            funding = _fetch_funding(symbol)
            msg = format_oi_spike_alert(symbol, chg, funding)
            _last_oi_alert[symbol] = now
            logger.info("OI spike %s: %+.2f%%", symbol, chg)
            alerts.append((symbol, msg, chg))

    return alerts
