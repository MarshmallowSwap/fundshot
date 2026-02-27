"""
alert_logic.py — Funding King Bot
Gestione soglie, anti-spam, cooldown, previsione prossimo intervallo.
"""

import os
import time
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Soglie (%) ────────────────────────────────────────────────────────────────
THRESHOLD_HARD        = 2.00
THRESHOLD_EXTREME     = 1.50
THRESHOLD_HIGH        = 1.00
THRESHOLD_CLOSE_TIP   = 0.23
THRESHOLD_RIENTRO     = 0.75
RESET_THRESHOLD       = 0.02
COOLDOWN_SECONDS      = int(os.getenv("COOLDOWN_SECONDS", 120))
FUNDING_ALERT_MINUTES = int(os.getenv("FUNDING_ALERT_MINUTES", 15))

# ── Simboli esclusi dal meccanismo automatico di cambio intervallo ─────────────
EXCLUDED_AUTO_INTERVAL = {
    "BTCUSDT", "BTCUSDC", "BTCUSD",
    "ETHUSDT", "ETHUSDC", "ETHUSD",
    "ETHBTCUSDT", "ETHWUSDT",
}

# ── Cap per simbolo (caricato dal bot al boot da instruments-info) ─────────────
# { "SOLUSDT": {"upperFundingRate": 0.02, "lowerFundingRate": -0.02,
#               "fundingInterval": 240} }
_symbol_caps: dict[str, dict] = {}


def set_symbol_caps(caps: dict[str, dict]):
    """Chiamato da bot.py al boot con i dati da get_instruments_info()."""
    global _symbol_caps
    _symbol_caps = caps
    logger.info("Cap simboli caricati: %d", len(caps))


def get_cap(symbol: str) -> float:
    """Restituisce il cap positivo del funding rate per il simbolo (default 2%)."""
    info = _symbol_caps.get(symbol, {})
    return float(info.get("upperFundingRate", 0.02))


# ── Previsione prossimo intervallo ────────────────────────────────────────────
def predict_next_interval(symbol: str, rate_pct: float, current_interval_h: int) -> tuple[str, bool]:
    """
    Prevede il prossimo intervallo di funding basandosi sulle regole Bybit.

    Regole:
      - Se |rate| >= cap del simbolo  → prossimo = 1H  (meccanismo automatico)
      - Se simbolo escluso            → rimane invariato sempre
      - Altrimenti                    → rimane invariato

    Restituisce:
      (label: str, changed: bool)
      es. ("1H", True) oppure ("4H", False)
    """
    if symbol in EXCLUDED_AUTO_INTERVAL:
        return (f"{current_interval_h}H", False)

    cap = get_cap(symbol) * 100  # converti in %
    if abs(rate_pct) >= cap:
        return ("1H", True)

    return (f"{current_interval_h}H", False)


# ── Stato per simbolo ─────────────────────────────────────────────────────────
_state: dict[str, dict] = {}


def _get_state(symbol: str) -> dict:
    if symbol not in _state:
        _state[symbol] = {
            "level": "none",
            "reset_time": 0.0,
            "next_funding_alerted": False,
        }
    return _state[symbol]


def get_all_states() -> dict:
    return {s: d for s, d in _state.items() if d["level"] != "none"}


def reset_state(symbol: str):
    _state[symbol] = {"level": "none", "reset_time": 0.0, "next_funding_alerted": False}


# ── Classificazione ───────────────────────────────────────────────────────────
def classify(rate_pct: float) -> str:
    abs_rate = abs(rate_pct)
    if abs_rate >= THRESHOLD_HARD:
        return "hard"
    if abs_rate >= THRESHOLD_EXTREME:
        return "extreme"
    if abs_rate >= THRESHOLD_HIGH:
        return "high"
    if abs_rate >= THRESHOLD_CLOSE_TIP:
        return "close_tip"
    return "none"


def _direction(rate_pct: float) -> str:
    return "SHORT" if rate_pct > 0 else "LONG"


def _interval_label(interval_h) -> str:
    try:
        return f"{int(interval_h)}H"
    except (TypeError, ValueError):
        return "—"


# ── Formattazione alert funding ───────────────────────────────────────────────
_LEVEL_META = {
    "hard":      ("🔴", "HARD FUNDING"),
    "extreme":   ("🔥", "EXTREME FUNDING"),
    "high":      ("🚨", "HIGH FUNDING"),
    "close_tip": ("ℹ️",  "CONSIGLIO CHIUSURA"),
    "rientro":   ("✅", "FUNDING RIENTRATO"),
}


def format_alert(
    symbol: str,
    rate_pct: float,
    interval_h,
    level: str,
    prev_level: str = "none",
) -> str:
    emoji, title = _LEVEL_META.get(level, ("📊", "FUNDING"))
    interval     = _interval_label(interval_h)
    rate_str     = f"{rate_pct:+.4f}%"
    direction    = _direction(rate_pct)

    if level == "rientro":
        return (
            f"{emoji} {title} — {symbol}\n"
            f"Rate: {rate_str} (ogni {interval})\n"
            f"Eccesso rientrato ✔"
        )

    if level == "close_tip":
        action = "chiudere posizioni SHORT" if rate_pct > 0 else "chiudere posizioni LONG"
        return (
            f"{emoji} {title} — {symbol}\n"
            f"Rate: {rate_str} (ogni {interval})\n"
            f"Valuta di {action}"
        )

    return (
        f"{emoji} {title} — {symbol}\n"
        f"Rate: {rate_str} (ogni {interval})\n"
        f"Segnale: ⚡ {direction}"
    )


# ── Formattazione alert prossimo funding ──────────────────────────────────────
def format_next_funding_alert(
    symbol: str,
    rate_pct: float,
    interval_h,
    minutes_left: int,
    next_funding_ts_ms: int,
) -> str:
    direction        = _direction(rate_pct)
    current_interval = _interval_label(interval_h)
    rate_str         = f"{rate_pct:+.4f}%"

    # Previsione prossimo intervallo
    next_interval_label, changed = predict_next_interval(symbol, rate_pct, int(interval_h))

    if changed:
        interval_line = f"Ciclo: {current_interval}  →  Prossimo: {next_interval_label} ⚠️"
    else:
        interval_line = f"Ciclo: {current_interval}  (invariato)"

    # Orario prossimo settlement
    settlement_dt   = datetime.fromtimestamp(next_funding_ts_ms / 1000, tz=timezone.utc)
    settlement_str  = settlement_dt.strftime("%H:%M UTC")

    return (
        f"⏰ FUNDING TRA {minutes_left} MIN — {symbol}\n"
        f"Rate: {rate_str}  |  {interval_line}\n"
        f"Segnale: ⚡ {direction}\n"
        f"Prossimo settlement: {settlement_str}"
    )


# ── Formattazione PUMP/DUMP ───────────────────────────────────────────────────
def format_pump_dump_alert(symbol: str, pct_1h: float, pct_24h: float, last_price: float) -> str:
    emoji = "🚀" if pct_1h > 0 else "💥"
    label = "PUMP" if pct_1h > 0 else "DUMP"
    return (
        f"{emoji} {label} — {symbol}\n"
        f"Prezzo: {last_price:,.2f} $\n"
        f"1H: {pct_1h:+.2f}%  |  24H: {pct_24h:+.2f}%"
    )


# ── Formattazione liquidazioni ────────────────────────────────────────────────
def format_liquidation_alert(symbol: str, side: str, size: float, usd_value: float) -> str:
    direction = "LONG liquidato" if side.upper() == "BUY" else "SHORT liquidato"
    return (
        f"💧 LIQUIDAZIONE — {symbol}\n"
        f"{direction}\n"
        f"Size: {size:.4f}  |  Valore: ${usd_value:,.0f}"
    )


# ── Logica principale funding (Opzione A) ─────────────────────────────────────
def process_funding(symbol: str, rate_pct: float, interval_h) -> str | None:
    """
    Opzione A: reset stato solo quando rate rientra sotto THRESHOLD_RIENTRO.
    Restituisce testo alert o None.
    """
    state    = _get_state(symbol)
    now      = time.monotonic()
    abs_rate = abs(rate_pct)

    # 1. Reset Bybit → cooldown
    if abs_rate < RESET_THRESHOLD:
        if state["level"] != "none" or state["reset_time"] == 0.0:
            state["level"]      = "none"
            state["reset_time"] = now
        return None

    # 2. Cooldown attivo → blocca tutto
    if state["reset_time"] and (now - state["reset_time"]) < COOLDOWN_SECONDS:
        return None

    # 3. Classifica
    new_level = classify(rate_pct)

    if new_level == "none":
        if state["level"] != "none" and abs_rate <= THRESHOLD_RIENTRO:
            prev = state["level"]
            state["level"] = "none"
            return format_alert(symbol, rate_pct, interval_h, "rientro", prev)
        return None

    prev_level = state["level"]

    # 4. Anti-spam: stesso livello → silenzio
    if new_level == prev_level:
        return None

    # 5. Nuovo/diverso livello → alert
    state["level"] = new_level
    return format_alert(symbol, rate_pct, interval_h, new_level, prev_level)


# ── Prossimo funding alert ────────────────────────────────────────────────────
def process_next_funding(
    symbol: str,
    rate_pct: float,
    interval_h,
    next_funding_ts_ms: int,
) -> str | None:
    """
    Alert se settlement entro FUNDING_ALERT_MINUTES e rate >= HIGH.
    Anti-spam: un solo alert per ciclo.
    """
    if abs(rate_pct) < THRESHOLD_HIGH:
        return None

    now_ms       = int(time.time() * 1000)
    minutes_left = (next_funding_ts_ms - now_ms) / 60000

    if minutes_left < 0 or minutes_left > FUNDING_ALERT_MINUTES:
        _get_state(symbol)["next_funding_alerted"] = False
        return None

    state = _get_state(symbol)
    if state["next_funding_alerted"]:
        return None

    state["next_funding_alerted"] = True
    return format_next_funding_alert(
        symbol, rate_pct, interval_h, int(minutes_left), next_funding_ts_ms
    )


# ── PUMP/DUMP ─────────────────────────────────────────────────────────────────
_pump_state: dict[str, float] = {}
PUMP_THRESHOLD_1H = float(os.getenv("PUMP_THRESHOLD_1H",  5.0))
DUMP_THRESHOLD_1H = float(os.getenv("DUMP_THRESHOLD_1H", -5.0))


def process_pump_dump(
    symbol: str,
    pct_1h_raw: str,
    pct_24h_raw: str,
    last_price_raw: str,
) -> str | None:
    try:
        pct_1h     = float(pct_1h_raw) * 100
        pct_24h    = float(pct_24h_raw) * 100
        last_price = float(last_price_raw)
    except (TypeError, ValueError):
        return None

    if pct_1h >= PUMP_THRESHOLD_1H or pct_1h <= DUMP_THRESHOLD_1H:
        prev = _pump_state.get(symbol, 0.0)
        if abs(pct_1h - prev) < 1.0:
            return None
        _pump_state[symbol] = pct_1h
        return format_pump_dump_alert(symbol, pct_1h, pct_24h, last_price)

    if symbol in _pump_state:
        del _pump_state[symbol]
    return None
