"""
oi_monitor.py — Monitoraggio OI spike su tutti i simboli perpetual USDT.
Alert indipendente dal funding rate.

Soglia default: spike >= 3% in 5min (o <= -3% per crollo)
"""
import logging
import time
import requests

logger = logging.getLogger(__name__)

# Soglia spike OI (configurabile)
OI_SPIKE_THRESHOLD = 3.0   # % in 5min
OI_DROP_THRESHOLD  = -3.0  # % in 5min (crollo)

# Cooldown per evitare spam: simbolo -> timestamp ultimo alert
_last_oi_alert: dict[str, float] = {}
OI_COOLDOWN_SEC = 300  # 5 min tra alert dello stesso simbolo


def _fetch_oi(symbol: str) -> dict | None:
    """Fetch OI 5min per un simbolo da Bybit."""
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
            curr  = float(items[0]["openInterest"])
            prev  = float(items[1]["openInterest"])
            chg   = (curr - prev) / prev * 100 if prev else 0
            return {"oi": curr, "change_5m": chg}
    except Exception as e:
        logger.debug("fetch_oi %s: %s", symbol, e)
    return None


def _fetch_funding(symbol: str) -> float | None:
    """Fetch funding rate corrente da Bybit (API pubblica)."""
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
    Genera suggerimento LONG/SHORT/NEUTRO basato su OI + funding.
    
    Logica:
    - OI ▲ spike + funding negativo → chi è short paga → LONG favorito
    - OI ▲ spike + funding positivo → chi è long paga  → SHORT favorito
    - OI ▲ spike + funding neutro   → pressione generica, direzione incerta
    - OI ▼ crollo                   → chiusura massiccia, evitare posizioni
    """
    if oi_chg <= OI_DROP_THRESHOLD:
        return "⚠️ Chiusura massiccia — evita nuove posizioni"

    if funding_pct is None:
        return "📊 OI in forte crescita — monitora direzione"

    abs_f = abs(funding_pct)

    # Soglie: anche funding piccolo indica già una direzione chiara
    if funding_pct < -0.01:
        strength = "forte" if abs_f >= 0.5 else "moderato" if abs_f >= 0.1 else "debole"
        return f"🟢 Consiglio: *LONG* — short pagano funding ({funding_pct:+.4f}%), segnale {strength}"
    elif funding_pct > 0.01:
        strength = "forte" if abs_f >= 0.5 else "moderato" if abs_f >= 0.1 else "debole"
        return f"🔴 Consiglio: *SHORT* — long pagano funding ({funding_pct:+.4f}%), segnale {strength}"
    else:
        return f"⚪ Funding neutro ({funding_pct:+.4f}%) — OI spike speculativo, cautela"


def format_oi_spike_alert(symbol: str, oi_chg: float, funding_pct: float | None) -> str:
    """Formatta il messaggio alert OI spike per Telegram."""
    arrow  = "▲" if oi_chg > 0 else "▼"
    emoji  = "⚡" if oi_chg >= OI_SPIKE_THRESHOLD else "📉"
    kind   = "SPIKE" if oi_chg >= OI_SPIKE_THRESHOLD else "CROLLO"
    f_str  = f"{funding_pct:+.4f}%" if funding_pct is not None else "n/d"
    suggestion = _get_suggestion(oi_chg, funding_pct)

    return (
        f"{emoji} *OI {kind}*\n"
        f"*{symbol}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 OI 5m:    `{arrow} {oi_chg:+.2f}%`\n"
        f"💸 Funding:  `{f_str}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{suggestion}"
    )


def check_oi_spikes(tickers: list) -> list[tuple[str, str, float]]:
    """
    Controlla spike OI su tutti i ticker passati.
    Ritorna lista di (symbol, alert_text, oi_change) per i simboli con spike.
    
    tickers: lista di dict con almeno {"symbol": str}
    """
    alerts = []
    now = time.monotonic()

    for ticker in tickers:
        symbol = ticker.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue

        # Cooldown
        if now - _last_oi_alert.get(symbol, 0) < OI_COOLDOWN_SEC:
            continue

        oi_data = _fetch_oi(symbol)
        if not oi_data:
            continue

        chg = oi_data["change_5m"]

        # Spike positivo o crollo
        if chg >= OI_SPIKE_THRESHOLD or chg <= OI_DROP_THRESHOLD:
            funding = _fetch_funding(symbol)
            msg = format_oi_spike_alert(symbol, chg, funding)
            _last_oi_alert[symbol] = now
            logger.info("OI spike %s: %+.2f%%", symbol, chg)
            alerts.append((symbol, msg, chg))

    return alerts
