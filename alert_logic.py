"""
alert_logic.py — Funding King Bot
Soglie ibride (fisse + dinamiche), anti-spam Opzione A, previsione intervallo.
"""

import os
import time
import logging
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURAZIONE SOGLIE
# ══════════════════════════════════════════════════════════════════════════════

# ── Soglie fisse (floor — non scendono mai sotto questi valori) ───────────────
THRESHOLD_HARD        = 2.00
THRESHOLD_EXTREME     = 1.50
THRESHOLD_HIGH        = 1.00
THRESHOLD_CLOSE_TIP   = 0.23
THRESHOLD_RIENTRO     = 0.75
RESET_THRESHOLD       = 0.02
COOLDOWN_SECONDS      = int(os.getenv("COOLDOWN_SECONDS", 120))
FUNDING_ALERT_MINUTES = int(os.getenv("FUNDING_ALERT_MINUTES", 15))

# ── Modalità ibrida ───────────────────────────────────────────────────────────
USE_DYNAMIC         = os.getenv("USE_DYNAMIC_THRESHOLDS", "false").lower() == "true"
DYNAMIC_WINDOW_H    = int(os.getenv("DYNAMIC_WINDOW_HOURS", 24))
MIN_SAMPLES_DYNAMIC = 10   # campioni minimi prima di attivare la dinamica

# ── Moltiplicatori per livello (applicati alla media rolling del simbolo) ─────
# soglia_effettiva = max(soglia_fissa, avg_rolling * moltiplicatore)
MULTIPLIERS = {
    "hard":      4.0,
    "extreme":   3.0,
    "high":      2.0,
    "close_tip": 1.5,
    "rientro":   1.2,
}

# ── Simboli esclusi dal meccanismo automatico Bybit ───────────────────────────
EXCLUDED_AUTO_INTERVAL = {
    "BTCUSDT", "BTCUSDC", "BTCUSD",
    "ETHUSDT", "ETHUSDC", "ETHUSD",
    "ETHBTCUSDT", "ETHWUSDT",
}

# ── Cap per simbolo (caricato al boot da instruments-info) ────────────────────
_symbol_caps: dict[str, dict] = {}


def set_symbol_caps(caps: dict[str, dict]):
    global _symbol_caps
    _symbol_caps = caps
    logger.info("Cap simboli caricati: %d", len(caps))


def get_cap(symbol: str) -> float:
    return float(_symbol_caps.get(symbol, {}).get("upperFundingRate", 0.02))


# ══════════════════════════════════════════════════════════════════════════════
# STORICO ROLLING PER SIMBOLO
# ══════════════════════════════════════════════════════════════════════════════

# { symbol: deque([(timestamp, abs_rate_pct), ...]) }
_rate_history: dict[str, deque] = {}


def update_rate_history(symbol: str, rate_pct: float):
    """
    Aggiunge il rate assoluto corrente allo storico rolling del simbolo.
    Rimuove automaticamente i campioni più vecchi di DYNAMIC_WINDOW_H ore.
    Chiamato dal funding_job ad ogni ciclo.
    """
    now = time.time()
    cutoff = now - DYNAMIC_WINDOW_H * 3600

    if symbol not in _rate_history:
        _rate_history[symbol] = deque()

    hist = _rate_history[symbol]
    hist.append((now, abs(rate_pct)))

    # Rimuovi campioni fuori dalla finestra
    while hist and hist[0][0] < cutoff:
        hist.popleft()


def get_avg_rolling(symbol: str) -> float:
    """
    Calcola la media del valore assoluto del funding rate
    nella finestra rolling configurata.
    Restituisce 0.0 se non ci sono abbastanza campioni.
    """
    hist = _rate_history.get(symbol)
    if not hist or len(hist) < MIN_SAMPLES_DYNAMIC:
        return 0.0
    return sum(r for _, r in hist) / len(hist)


def get_history_stats(symbol: str) -> dict:
    """Statistiche rolling per /status e debug."""
    hist = _rate_history.get(symbol)
    if not hist:
        return {"samples": 0, "avg": 0.0, "max": 0.0, "min": 0.0}
    rates = [r for _, r in hist]
    return {
        "samples": len(rates),
        "avg":     round(sum(rates) / len(rates), 4),
        "max":     round(max(rates), 4),
        "min":     round(min(rates), 4),
        "window_h": DYNAMIC_WINDOW_H,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SOGLIE EFFETTIVE (IBRIDO)
# ══════════════════════════════════════════════════════════════════════════════

_FIXED_THRESHOLDS = {
    "hard":      THRESHOLD_HARD,
    "extreme":   THRESHOLD_EXTREME,
    "high":      THRESHOLD_HIGH,
    "close_tip": THRESHOLD_CLOSE_TIP,
    "rientro":   THRESHOLD_RIENTRO,
}


def get_effective_threshold(symbol: str, level: str) -> float:
    """
    Calcola la soglia effettiva per il simbolo e il livello.

    Logica ibrida:
      soglia_eff = max(soglia_fissa, avg_rolling * moltiplicatore)

    Se USE_DYNAMIC=false o campioni insufficienti → usa solo soglia fissa.
    La soglia fissa fa sempre da FLOOR (non scende mai sotto).
    """
    base = _FIXED_THRESHOLDS.get(level, 1.0)

    if not USE_DYNAMIC:
        return base

    avg = get_avg_rolling(symbol)
    if avg == 0.0:
        return base   # storico insufficiente → fallback a fissa

    dynamic = avg * MULTIPLIERS.get(level, 2.0)
    effective = max(base, dynamic)

    if effective > base:
        logger.debug(
            "%s [%s]: soglia dinamica %.4f%% > floor %.4f%%",
            symbol, level, effective, base
        )

    return effective


def get_thresholds_info(symbol: str) -> dict:
    """
    Restituisce un dict con soglie fisse, dinamiche ed effettive per il simbolo.
    Utile per /status e debug.
    """
    avg = get_avg_rolling(symbol) if USE_DYNAMIC else 0.0
    result = {"dynamic_active": USE_DYNAMIC, "avg_rolling": avg, "levels": {}}
    for level in ["hard", "extreme", "high", "close_tip"]:
        fixed     = _FIXED_THRESHOLDS[level]
        dynamic   = avg * MULTIPLIERS[level] if avg > 0 else 0.0
        effective = max(fixed, dynamic) if USE_DYNAMIC and avg > 0 else fixed
        result["levels"][level] = {
            "fixed":     fixed,
            "dynamic":   round(dynamic, 4),
            "effective": round(effective, 4),
            "source":    "dinamica" if effective > fixed else "fissa",
        }
    return result


# ══════════════════════════════════════════════════════════════════════════════
# CLASSIFICAZIONE
# ══════════════════════════════════════════════════════════════════════════════

def classify(symbol: str, rate_pct: float) -> str:
    """
    Classifica il funding rate usando le soglie effettive (ibride).
    """
    abs_rate = abs(rate_pct)

    if abs_rate >= get_effective_threshold(symbol, "hard"):
        return "hard"
    if abs_rate >= get_effective_threshold(symbol, "extreme"):
        return "extreme"
    if abs_rate >= get_effective_threshold(symbol, "high"):
        return "high"
    if abs_rate >= get_effective_threshold(symbol, "close_tip"):
        return "close_tip"
    return "none"


def _direction(rate_pct: float) -> str:
    return "SHORT" if rate_pct > 0 else "LONG"


def _interval_label(interval_h) -> str:
    try:
        return f"{int(interval_h)}H"
    except (TypeError, ValueError):
        return "—"


# ══════════════════════════════════════════════════════════════════════════════
# FORMATTAZIONE ALERT
# ══════════════════════════════════════════════════════════════════════════════

_LEVEL_META = {
    "hard":      ("🔴", "HARD FUNDING"),
    "extreme":   ("🔥", "EXTREME FUNDING"),
    "high":      ("🚨", "HIGH FUNDING"),
    "close_tip": ("ℹ️",  "CONSIGLIO CHIUSURA"),
    "rientro":   ("✅", "FUNDING RIENTRATO"),
}


def _dynamic_suffix(symbol: str, level: str) -> str:
    """Aggiunge [D] se la soglia usata è dinamica, [F] se fissa."""
    if not USE_DYNAMIC:
        return ""
    info = get_thresholds_info(symbol)["levels"].get(level, {})
    if info.get("source") == "dinamica":
        thr = info.get("effective", 0)
        return f"  _(soglia: {thr:.2f}%)_"
    return ""


def format_alert(
    symbol: str,
    rate_pct: float,
    interval_h,
    level: str,
    prev_level: str = "none",
) -> str:
    emoji, title = _LEVEL_META.get(level, ("📊", "FUNDING"))
    interval  = _interval_label(interval_h)
    rate_str  = f"{rate_pct:+.4f}%"
    direction = _direction(rate_pct)
    suffix    = _dynamic_suffix(symbol, level)

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
            f"Rate: {rate_str} (ogni {interval}){suffix}\n"
            f"Valuta di {action}"
        )

    return (
        f"{emoji} {title} — {symbol}\n"
        f"Rate: {rate_str} (ogni {interval}){suffix}\n"
        f"Segnale: ⚡ {direction}"
    )


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

    next_interval_label, changed = predict_next_interval(symbol, rate_pct, int(interval_h))
    interval_line = (
        f"Ciclo: {current_interval}  →  Prossimo: {next_interval_label} ⚠️"
        if changed else
        f"Ciclo: {current_interval}  (invariato)"
    )

    settlement_dt  = datetime.fromtimestamp(next_funding_ts_ms / 1000, tz=timezone.utc)
    settlement_str = settlement_dt.strftime("%H:%M UTC")

    return (
        f"⏰ FUNDING TRA {minutes_left} MIN — {symbol}\n"
        f"Rate: {rate_str}  |  {interval_line}\n"
        f"Segnale: ⚡ {direction}\n"
        f"Prossimo settlement: {settlement_str}"
    )


def format_pump_dump_alert(symbol: str, pct_1h: float, pct_24h: float, last_price: float) -> str:
    emoji = "🚀" if pct_1h > 0 else "💥"
    label = "PUMP" if pct_1h > 0 else "DUMP"
    return (
        f"{emoji} {label} — {symbol}\n"
        f"Prezzo: {last_price:,.2f} $\n"
        f"1H: {pct_1h:+.2f}%  |  24H: {pct_24h:+.2f}%"
    )


def format_liquidation_alert(symbol: str, side: str, size: float, usd_value: float) -> str:
    direction = "LONG liquidato" if side.upper() == "BUY" else "SHORT liquidato"
    return (
        f"💧 LIQUIDAZIONE — {symbol}\n"
        f"{direction}\n"
        f"Size: {size:.4f}  |  Valore: ${usd_value:,.0f}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# PREVISIONE PROSSIMO INTERVALLO
# ══════════════════════════════════════════════════════════════════════════════

def predict_next_interval(symbol: str, rate_pct: float, current_interval_h: int) -> tuple[str, bool]:
    if symbol in EXCLUDED_AUTO_INTERVAL:
        return (f"{current_interval_h}H", False)
    cap = get_cap(symbol) * 100
    if abs(rate_pct) >= cap:
        return ("1H", True)
    return (f"{current_interval_h}H", False)


# ══════════════════════════════════════════════════════════════════════════════
# STATO PER SIMBOLO (anti-spam Opzione A)
# ══════════════════════════════════════════════════════════════════════════════

_state: dict[str, dict] = {}


def _get_state(symbol: str) -> dict:
    if symbol not in _state:
        _state[symbol] = {
            "level":                "none",
            "reset_time":           0.0,
            "next_funding_alerted": False,
        }
    return _state[symbol]


def get_all_states() -> dict:
    return {s: d for s, d in _state.items() if d["level"] != "none"}


def reset_state(symbol: str):
    _state[symbol] = {"level": "none", "reset_time": 0.0, "next_funding_alerted": False}


# ══════════════════════════════════════════════════════════════════════════════
# LOGICA PRINCIPALE FUNDING (Opzione A)
# ══════════════════════════════════════════════════════════════════════════════

def process_funding(symbol: str, rate_pct: float, interval_h) -> str | None:
    """
    Opzione A: reset stato solo quando rate <= THRESHOLD_RIENTRO (effettivo).
    Usa soglie ibride (fisse + dinamiche).
    """
    state    = _get_state(symbol)
    now      = time.monotonic()
    abs_rate = abs(rate_pct)

    # 1. Reset Bybit (rate quasi zero) → cooldown
    if abs_rate < RESET_THRESHOLD:
        if state["level"] != "none" or state["reset_time"] == 0.0:
            state["level"]      = "none"
            state["reset_time"] = now
        return None

    # 2. Cooldown attivo → blocca tutto
    if state["reset_time"] and (now - state["reset_time"]) < COOLDOWN_SECONDS:
        return None

    # 3. Classifica con soglie effettive
    new_level  = classify(symbol, rate_pct)
    prev_level = state["level"]

    if new_level == "none":
        # Controlla rientro con soglia effettiva
        rientro_thr = get_effective_threshold(symbol, "rientro")
        if prev_level != "none" and abs_rate <= rientro_thr:
            state["level"] = "none"
            return format_alert(symbol, rate_pct, interval_h, "rientro", prev_level)
        return None

    # 4. Anti-spam: stesso livello → silenzio
    if new_level == prev_level:
        return None

    # 5. Livello nuovo o upgrade → alert
    state["level"] = new_level

    # close_tip: invia solo se il simbolo è già "funded" (ha avuto HIGH+)
    # o se esplicitamente monitored via watchlist (check esterno in bot.py)
    if new_level == "close_tip" and not is_funded(symbol):
        return None  # sopprimi close_tip per simboli non ancora allertati

    # Segna il simbolo come funded per HIGH / EXTREME / HARD
    if new_level in ("high", "extreme", "hard"):
        mark_funded(symbol)

    return format_alert(symbol, rate_pct, interval_h, new_level, prev_level)


# ══════════════════════════════════════════════════════════════════════════════
# PROSSIMO FUNDING ALERT
# ══════════════════════════════════════════════════════════════════════════════

def process_next_funding(
    symbol: str,
    rate_pct: float,
    interval_h,
    next_funding_ts_ms: int,
) -> str | None:
    high_thr = get_effective_threshold(symbol, "high")
    if abs(rate_pct) < high_thr:
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


# ══════════════════════════════════════════════════════════════════════════════
# PUMP / DUMP
# ══════════════════════════════════════════════════════════════════════════════

_pump_state: dict[str, float] = {}
PUMP_THRESHOLD_1H = float(os.getenv("PUMP_THRESHOLD_1H",  5.0))
DUMP_THRESHOLD_1H = float(os.getenv("DUMP_THRESHOLD_1H", -5.0))

# ──────────────────────────────────────────────────────────────────────────────
# FUNDED SYMBOLS — set di simboli che hanno ricevuto alert HIGH/EXTREME/HARD
# Usato per filtrare PUMP/DUMP e CLOSE_TIP solo su coppie rilevanti
# ──────────────────────────────────────────────────────────────────────────────
_funded_symbols: set[str] = set()

def mark_funded(symbol: str) -> None:
    """Registra che il simbolo ha ricevuto almeno un alert HIGH/EXTREME/HARD."""
    _funded_symbols.add(symbol)

def is_funded(symbol: str) -> bool:
    """True se il simbolo ha ricevuto un alert HIGH/EXTREME/HARD in questa sessione."""
    return symbol in _funded_symbols

def get_funded_symbols() -> set[str]:
    """Restituisce copia del set di simboli funded."""
    return set(_funded_symbols)


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
