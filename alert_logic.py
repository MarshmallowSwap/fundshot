"""
alert_logic.py — FundShot Bot
Soglie ibride (fisse + dinamiche), anti-spam Opzione A, previsione intervallo.

MODIFICHE RISPETTO ALL'ORIGINALE:
  - Alert PERICOLO CHIUSURA (warn_tip  >= 0.25%) → SOPPRESSO completamente
  - Alert FUNDING RIENTRATO (rientro   <= 0.20%) → SOPPRESSO completamente
"""

import os
import requests
import time
import logging
from collections import deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


# ── Alert Config Manager (sincronizzazione dashboard) ─────────────────────────
try:
    import alert_config_manager as _acm
    _ACM_AVAILABLE = True
except ImportError:
    _acm = None
    _ACM_AVAILABLE = False

def _alert_enabled(alert_type: str) -> bool:
    if _ACM_AVAILABLE and _acm:
        return _acm.is_enabled(alert_type)
    return True


TZ_IT = ZoneInfo("Europe/Rome")

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURAZIONE SOGLIE
# ══════════════════════════════════════════════════════════════════════════════

# ── Soglie fisse (floor — non scendono mai sotto questi valori) ───────────────
THRESHOLD_CRITICO     = 2.50   # CRITICO: funding jackpot — massima opportunita di incasso
THR_JACKPOT   = THRESHOLD_CRITICO  # alias usato da commands.py
THR_CRITICO   = THRESHOLD_CRITICO
THR_EXTREME   = 1.50
THR_HARD      = 2.00
THR_HIGH      = 1.00
THR_CLOSE_TIP = 0.75
THRESHOLD_HARD        = 2.00
THRESHOLD_EXTREME     = 1.50
THRESHOLD_BASE        = 0.50
THRESHOLD_HIGH        = 1.00
THRESHOLD_CLOSE_TIP   = 0.75   # CONSIGLIO CHIUSURA: funding rientro area
THRESHOLD_WARN_TIP    = 0.25   # mantenuto per compatibilita ma NON invia alert
THRESHOLD_RIENTRO     = 0.20   # mantenuto per compatibilita ma NON invia alert
RESET_THRESHOLD       = 0.02
COOLDOWN_SECONDS      = int(os.getenv("COOLDOWN_SECONDS", 120))
FUNDING_ALERT_MINUTES = int(os.getenv("FUNDING_ALERT_MINUTES", 15))

# ── Modalita ibrida ───────────────────────────────────────────────────────────
USE_DYNAMIC         = os.getenv("USE_DYNAMIC_THRESHOLDS", "false").lower() == "true"
DYNAMIC_WINDOW_H    = int(os.getenv("DYNAMIC_WINDOW_HOURS", 24))
MIN_SAMPLES_DYNAMIC = 10   # campioni minimi prima di attivare la dinamica

# ── Moltiplicatori per livello (applicati alla media rolling del simbolo) ─────
# soglia_effettiva = max(soglia_fissa, avg_rolling * moltiplicatore)
MULTIPLIERS = {
    "critico":   5.0,
    "hard":      4.0,
    "extreme":   3.0,
    "high":      2.0,
    "close_tip": 1.5,
    "warn_tip":  1.1,
    "rientro":   1.0,
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
    Rimuove automaticamente i campioni piu vecchi di DYNAMIC_WINDOW_H ore.
    Chiamato dal funding_job ad ogni ciclo.
    """
    now = time.time()
    cutoff = now - DYNAMIC_WINDOW_H * 3600

    if symbol not in _rate_history:
        _rate_history[symbol] = deque()

    hist = _rate_history[symbol]
    hist.append((now, abs(rate_pct)))

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
        "samples":  len(rates),
        "avg":      round(sum(rates) / len(rates), 4),
        "max":      round(max(rates), 4),
        "min":      round(min(rates), 4),
        "window_h": DYNAMIC_WINDOW_H,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SOGLIE EFFETTIVE (IBRIDO)
# ══════════════════════════════════════════════════════════════════════════════

_FIXED_THRESHOLDS = {
    "critico":   THRESHOLD_CRITICO,
    "hard":      THRESHOLD_HARD,
    "extreme":   THRESHOLD_EXTREME,
    "high":      THRESHOLD_HIGH,
    "soft":      THRESHOLD_BASE,
    "close_tip": THRESHOLD_CLOSE_TIP,
    "warn_tip":  THRESHOLD_WARN_TIP,
    "rientro":   THRESHOLD_RIENTRO,
}


def get_effective_threshold(symbol: str, level: str) -> float:
    """
    Calcola la soglia effettiva per il simbolo e il livello.

    Logica ibrida:
      soglia_eff = max(soglia_fissa, avg_rolling * moltiplicatore)

    Se USE_DYNAMIC=false o campioni insufficienti -> usa solo soglia fissa.
    La soglia fissa fa sempre da FLOOR (non scende mai sotto).
    """
    base = _FIXED_THRESHOLDS.get(level, 1.0)

    if not USE_DYNAMIC:
        return base

    avg = get_avg_rolling(symbol)
    if avg == 0.0:
        return base

    dynamic   = avg * MULTIPLIERS.get(level, 2.0)
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
    avg    = get_avg_rolling(symbol) if USE_DYNAMIC else 0.0
    result = {"dynamic_active": USE_DYNAMIC, "avg_rolling": avg, "levels": {}}
    for level in ["critico", "hard", "extreme", "high", "soft", "close_tip", "warn_tip"]:
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
    """Classifica il funding rate usando le soglie effettive (ibride)."""
    abs_rate = abs(rate_pct)

    if abs_rate >= get_effective_threshold(symbol, "critico"):
        return "critico"
    if abs_rate >= get_effective_threshold(symbol, "hard"):
        return "hard"
    if abs_rate >= get_effective_threshold(symbol, "extreme"):
        return "extreme"
    if abs_rate >= get_effective_threshold(symbol, "high"):
        return "high"
    if abs_rate >= get_effective_threshold(symbol, "soft"):
        return "soft"
    if abs_rate >= get_effective_threshold(symbol, "close_tip"):
        return "close_tip"
    if abs_rate >= get_effective_threshold(symbol, "warn_tip"):
        return "warn_tip"
    return "none"


def _direction(rate_pct: float) -> str:
    return "SHORT" if rate_pct > 0 else "LONG"


def _interval_label(interval_h) -> str:
    try:
        return f"{int(interval_h)}H"
    except (TypeError, ValueError):
        return "---"


# ══════════════════════════════════════════════════════════════════════════════
# FORMATTAZIONE ALERT
# ══════════════════════════════════════════════════════════════════════════════

_LEVEL_META = {
    "critico":   ("\U0001f911", "\U0001f48e JACKPOT FUNDING \U0001f48e"),
    "hard":      ("\U0001f534", "HARD FUNDING"),
    "extreme":   ("\U0001f525", "EXTREME FUNDING"),
    "high":      ("\U0001f6a8", "HIGH FUNDING"),
    "soft":      ("\U0001f4ca", "SOFT FUNDING"),
    "close_tip": ("\u26a0\ufe0f",  "CONSIGLIO CHIUSURA"),
    "warn_tip":  ("\u2139\ufe0f",  "PERICOLO CHIUSURA"),
    "rientro":   ("\u2705", "FUNDING RIENTRATO"),
}


def _dynamic_suffix(symbol: str, level: str) -> str:
    """Aggiunge (D) se la soglia usata e dinamica."""
    if not USE_DYNAMIC:
        return ""
    info = get_thresholds_info(symbol)["levels"].get(level, {})
    if info.get("source") == "dinamica":
        thr = info.get("effective", 0)
        return f"  _(soglia: {thr:.2f}%)_"
    return ""



def _get_oi(symbol: str) -> str:
    """Fetch OI change 5m dalla API pubblica Bybit. Restituisce stringa formattata."""
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/open-interest",
            params={"category": "linear", "symbol": symbol,
                    "intervalTime": "5min", "limit": 3},
            timeout=5
        )
        data = r.json()
        if data.get("retCode") == 0:
            items = data["result"]["list"]
            curr  = float(items[0]["openInterest"])
            prev  = float(items[1]["openInterest"])
            chg   = (curr - prev) / prev * 100 if prev else 0
            arrow = "▲" if chg >= 0 else "▼"
            return f"{arrow} `{chg:+.2f}%`"
    except Exception:
        pass
    return "`n/d`"


def format_alert(
    symbol: str,
    rate_pct: float,
    interval_h,
    level: str,
    prev_level: str = "none",
    last_price: float = 0.0,
    pct_24h: float = 0.0,
) -> str:
    emoji, title = _LEVEL_META.get(level, ("\U0001f4ca", "FUNDING"))
    interval  = _interval_label(interval_h)
    rate_str  = f"{rate_pct:+.4f}%"
    direction = _direction(rate_pct)
    suffix    = _dynamic_suffix(symbol, level)
    oi_str    = _get_oi(symbol)

    # Righe comuni prezzo e 24h
    price_str = f"`${last_price:.6f}`" if last_price > 0 else "—"
    p24_arrow = "▲" if pct_24h >= 0 else "▼"
    p24_color = "+" if pct_24h >= 0 else ""
    p24_str   = f"{p24_arrow} `{p24_color}{pct_24h:.2f}%`" if pct_24h != 0 else "—"
    price_line = f"💵 Price:  {price_str}  |  24h: {p24_str}\n" if last_price > 0 else ""

    if level == "rientro":
        return (
            f"{emoji} *{title}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 *{symbol}*\n"
            f"📊 Rate:    `{rate_str}` (ogni {interval})\n"
            f"{price_line}"
            f"📈 OI 5m:   {oi_str}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"✅ Excess cleared — positions safe"
        )

    if level == "critico":
        side   = "SHORT" if rate_pct > 0 else "LONG"
        income = "SHORTs collect funding" if rate_pct > 0 else "LONGs collect funding"
        return (
            f"{emoji} *{title}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 *{symbol}*\n"
            f"📊 Rate:    `{rate_str}` (ogni {interval}){suffix}\n"
            f"{price_line}"
            f"📈 OI 5m:   {oi_str}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 MAX FUNDING — {income}\n"
            f"🚀 Hold / Open *{side}* — RARE opportunity!"
        )

    if level == "close_tip":
        action = "close SHORT" if rate_pct > 0 else "close LONG"
        return (
            f"{emoji} *{title}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 *{symbol}*\n"
            f"📊 Rate:    `{rate_str}` (ogni {interval}){suffix}\n"
            f"{price_line}"
            f"📈 OI 5m:   {oi_str}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔔 Consider {action} — funding retreating"
        )

    if level == "warn_tip":
        side = "SHORT" if rate_pct > 0 else "LONG"
        return (
            f"{emoji} *{title}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 *{symbol}*\n"
            f"📊 Rate:    `{rate_str}` (ogni {interval}){suffix}\n"
            f"{price_line}"
            f"📈 OI 5m:   {oi_str}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ Funding on {side} still active — monitor"
        )

    # BASE / HIGH / EXTREME / HARD
    return (
        f"{emoji} *{title}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 *{symbol}*\n"
        f"📊 Rate:    `{rate_str}` (ogni {interval}){suffix}\n"
        f"{price_line}"
        f"📈 OI 5m:   {oi_str}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Signal: {direction}"
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
        f"Cycle: {current_interval}  ->  Next: {next_interval_label} (change!)"
        if changed else
        f"Cycle: {current_interval}  (unchanged)"
    )

    settlement_dt  = datetime.fromtimestamp(next_funding_ts_ms / 1000, tz=TZ_IT)
    settlement_str = settlement_dt.strftime("%H:%M %Z")

    return (
        f"FUNDING TRA {minutes_left} MIN -- {symbol}\n"
        f"Rate: {rate_str}  |  {interval_line}\n"
        f"Signal: {direction}\n"
        f"Next settlement: {settlement_str}"
    )


def format_pump_dump_alert(symbol: str, pct_1h: float, pct_24h: float, last_price: float) -> str:
    emoji = "\U0001f680" if pct_1h > 0 else "\U0001f4a5"
    label = "PUMP" if pct_1h > 0 else "DUMP"
    return (
        f"{emoji} {label} -- {symbol}\n"
        f"Price: {last_price:,.2f} $\n"
        f"1H: {pct_1h:+.2f}%  |  24H: {pct_24h:+.2f}%"
    )


def format_liquidation_alert(symbol: str, side: str, size: float, usd_value: float) -> str:
    direction = "LONG liquidato" if side.upper() == "BUY" else "SHORT liquidato"
    return (
        f"\U0001f4a7 LIQUIDAZIONE -- {symbol}\n"
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

# Tempo minimo (secondi) prima di re-inviare lo STESSO livello per lo stesso simbolo
MIN_RESEND_INTERVAL: dict[str, int] = {
    "critico":   600,   # 10 min
    "hard":      600,   # 10 min
    "extreme":   600,   # 10 min
    "high":      600,   # 10 min
    "soft":      300,   #  5 min
    "close_tip": 300,   #  5 min
    "warn_tip":  300,   #  5 min
    "rientro":   300,   #  5 min
}

# _last_alert_time[symbol][level] = time.monotonic() dell'ultimo invio
_last_alert_time: dict[str, dict[str, float]] = {}

# Ultimo tasso non-zero per simbolo (usato da funding_tracker per il calcolo gain)
_last_nonzero_rate: dict[str, float] = {}


def get_last_nonzero_rate(symbol: str) -> float:
    """Restituisce l'ultimo funding rate significativo prima del reset."""
    return _last_nonzero_rate.get(symbol, 0.0)


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


# ── FUNDED SYMBOLS ────────────────────────────────────────────────────────────
# Set di simboli che hanno ricevuto alert HIGH/EXTREME/HARD.
# Usato per filtrare CLOSE_TIP solo su coppie rilevanti.
_funded_symbols: set[str] = set()


def _can_send_alert(symbol: str, level: str) -> bool:
    """True se non abbiamo gia inviato questo livello per questo simbolo
    nell'ultimo MIN_RESEND_INTERVAL[level] secondi."""
    min_interval = MIN_RESEND_INTERVAL.get(level, 0)
    if min_interval == 0:
        return True
    last = _last_alert_time.get(symbol, {}).get(level, 0.0)
    return (time.monotonic() - last) >= min_interval


def _record_alert(symbol: str, level: str) -> None:
    """Registra l'istante di invio per symbol+level."""
    if symbol not in _last_alert_time:
        _last_alert_time[symbol] = {}
    _last_alert_time[symbol][level] = time.monotonic()


def mark_funded(symbol: str) -> None:
    """Registra che il simbolo ha ricevuto almeno un alert HIGH/EXTREME/HARD."""
    _funded_symbols.add(symbol)


def is_funded(symbol: str) -> bool:
    """True se il simbolo ha ricevuto un alert HIGH/EXTREME/HARD in questa sessione."""
    return symbol in _funded_symbols


def get_funded_symbols() -> set[str]:
    """Restituisce copia del set di simboli funded."""
    return set(_funded_symbols)


# ══════════════════════════════════════════════════════════════════════════════
# LOGICA PRINCIPALE FUNDING (Opzione A)
# ══════════════════════════════════════════════════════════════════════════════

def process_funding(symbol: str, rate_pct: float, interval_h, last_price: float = 0.0, pct_24h: float = 0.0) -> str | None:
    """
    Opzione A: reset stato solo quando rate <= THRESHOLD_RIENTRO (effettivo).
    Usa soglie ibride (fisse + dinamiche).

    Alert SOPPRESSI:
      - warn_tip  (PERICOLO CHIUSURA) -> mai inviato
      - rientro   (FUNDING RIENTRATO) -> mai inviato
    """
    state    = _get_state(symbol)
    now      = time.monotonic()
    abs_rate = abs(rate_pct)

    # 1. Reset Bybit (rate quasi zero) -> cooldown
    if abs_rate < RESET_THRESHOLD:
        if state["level"] != "none" or state["reset_time"] == 0.0:
            state["level"]      = "none"
            state["reset_time"] = now
        return None

    # 2. Cooldown attivo -> blocca tutto
    if state["reset_time"] and (now - state["reset_time"]) < COOLDOWN_SECONDS:
        return None

    # 3. Classifica con soglie effettive
    _last_nonzero_rate[symbol] = rate_pct
    new_level  = classify(symbol, rate_pct)
    prev_level = state["level"]

    # ── MODIFICA 1: FUNDING RIENTRATO soppresso ───────────────────────────────
    if new_level == "none":
        rientro_thr = get_effective_threshold(symbol, "rientro")
        if prev_level != "none" and abs_rate <= rientro_thr:
            state["level"] = "none"
        return None   # alert FUNDING RIENTRATO rimosso: non inviare mai
    # ─────────────────────────────────────────────────────────────────────────

    # 4. Anti-spam: stesso livello -> silenzio
    if new_level == prev_level:
        return None

    # 5. Livello nuovo o upgrade -> alert
    state["level"] = new_level

    # ── MODIFICA 2: PERICOLO CHIUSURA soppresso ───────────────────────────────
    if new_level == "warn_tip":
        return None   # alert PERICOLO CHIUSURA rimosso: non inviare mai

    if new_level == "close_tip" and not is_funded(symbol):
        return None   # CONSIGLIO CHIUSURA solo se simbolo gia funded
    # ─────────────────────────────────────────────────────────────────────────

    # Segna il simbolo come funded per CRITICO / HIGH / EXTREME / HARD
    if new_level in ("critico", "high", "extreme", "hard", "soft"):
        mark_funded(symbol)

    # Anti-duplicato temporale
    if not _can_send_alert(symbol, new_level):
        return None
    _record_alert(symbol, new_level)

    return format_alert(symbol, rate_pct, interval_h, new_level, prev_level,
                        last_price=last_price, pct_24h=pct_24h)


# ══════════════════════════════════════════════════════════════════════════════
# PROSSIMO FUNDING ALERT
# ══════════════════════════════════════════════════════════════════════════════

def process_next_funding(
    symbol: str,
    rate_pct: float,
    interval_h,
    next_funding_ts_ms: int,
    last_price: float = 0.0,
    pct_24h: float = 0.0,
) -> str | None:
    """Alert unificato PRE-SETTLEMENT: countdown + next funding + suggerimento.
    Scatta FUNDING_ALERT_MINUTES minuti prima del settlement per tutti i simboli
    con funding significativo (>= base) o con posizione aperta.
    """
    if not _alert_enabled('next_funding'):
        return None

    base_thr = get_effective_threshold(symbol, "soft")
    funded   = is_funded(symbol)

    # Invia se ha posizione aperta OPPURE se funding >= base
    if not funded and abs(rate_pct) < base_thr:
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

    # Componi alert unificato
    level        = classify(symbol, rate_pct)
    EMOJI_LVL    = {"soft":"📊","warn_tip":"⚠️","close_tip":"🔔","high":"🚨","extreme":"🔥","hard":"🔴","critico":"🎰"}
    LBL          = {"soft":"SOFT","warn_tip":"WARN","close_tip":"CLOSE","high":"HIGH","extreme":"EXTREME","hard":"HARD","critico":"JACKPOT"}
    lvl_emoji    = EMOJI_LVL.get(level, "📊")
    lvl_lbl      = LBL.get(level, level.upper())
    direction    = _direction(rate_pct)
    sign         = "+" if rate_pct >= 0 else ""

    # Next interval prediction
    next_lbl, changed = predict_next_interval(symbol, rate_pct, int(interval_h))
    interval_line = f"⏭ Next cycle: `{next_lbl}` ⚠️ change!" if changed else f"⏭ Next cycle: `{next_lbl}`"

    # Suggerimento basato su posizione + rate
    if funded:
        if abs(rate_pct) >= get_effective_threshold(symbol, "extreme"):
            suggerimento = "💰 HOLD — high funding, next collection imminent"
        elif abs(rate_pct) >= get_effective_threshold(symbol, "soft"):
            suggerimento = "👀 MONITOR — consider holding after settlement"
        else:
            suggerimento = "🔔 CAUTION — funding retreating, consider closing"
    else:
        if abs(rate_pct) >= get_effective_threshold(symbol, "high"):
            suggerimento = f"🎯 OPPORTUNITY — consider opening {direction}"
        else:
            suggerimento = f"📊 Signal {direction} — watch after reset"

    settlement_dt  = datetime.fromtimestamp(next_funding_ts_ms / 1000, tz=TZ_IT)
    settlement_str = settlement_dt.strftime("%H:%M")

    pos_line  = "✅ Position open" if funded else "📭 No position"
    price_str = f"`${last_price:.6f}`" if last_price > 0 else "—"
    p24_arrow = "▲" if pct_24h >= 0 else "▼"
    p24_str   = f"{p24_arrow} `{('+' if pct_24h>=0 else '')}{pct_24h:.2f}%`" if pct_24h != 0 else ""
    price_line = f"💵 Price:  {price_str}  |  24h: {p24_str}\n" if last_price > 0 else ""

    return (
        f"⏰ *PRE-SETTLEMENT — {int(minutes_left)} MIN*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 *{symbol}*  {lvl_emoji} `{lvl_lbl}`\n"
        f"📊 Funding:  `{sign}{rate_pct:.4f}%`  |  {direction}\n"
        f"{price_line}"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🕐 Settlement: `{settlement_str}`\n"
        f"{interval_line}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{pos_line}\n"
        f"{suggerimento}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# PUMP / DUMP
# ══════════════════════════════════════════════════════════════════════════════

_pump_state: dict[str, float] = {}
PUMP_THRESHOLD_1H = float(os.getenv("PUMP_THRESHOLD_1H",  5.0))
DUMP_THRESHOLD_1H = float(os.getenv("DUMP_THRESHOLD_1H", -5.0))


def process_pump_dump(
    symbol: str,
    pct_1h_raw: str,
    pct_24h_raw: str,
    last_price_raw: str,
) -> str | None:
    if not _alert_enabled('pump_dump'):
        return None
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


# ══════════════════════════════════════════════════════════════════════════════
# CAMBIO LIVELLO
# ══════════════════════════════════════════════════════════════════════════════

_prev_level_map:       dict[str, str]   = {}
_prev_rate_map:        dict[str, float]  = {}
_level_change_cooldown: dict[str, float] = {}
_LEVEL_CHANGE_CD_SEC = 300   # 5 minuti


def check_level_change(symbol: str, new_level: str, rate_pct: float = 0.0, prev_rate_pct: float = 0.0,
                        last_price: float = 0.0, pct_24h: float = 0.0) -> str | None:
    if not _alert_enabled('level_change'):
        return None

    prev = _prev_level_map.get(symbol, 'none')
    prev_rate_saved = _prev_rate_map.get(symbol, 0.0)
    _prev_level_map[symbol] = new_level
    _prev_rate_map[symbol]  = rate_pct

    if prev == new_level or prev == 'none' or new_level == 'none':
        return None

    now = time.time()
    if now - _level_change_cooldown.get(symbol, 0) < _LEVEL_CHANGE_CD_SEC:
        return None
    _level_change_cooldown[symbol] = now

    RANK = {
        'none': 0, 'rientro': 0, 'soft': 1, 'warn_tip': 2,
        'close_tip': 3, 'high': 4, 'extreme': 5, 'hard': 6, 'critico': 7,
    }
    EMOJI = {
        'soft':      '📊',
        'warn_tip':  '⚠️',
        'close_tip': '🔔',
        'high':      '🚨',
        'extreme':   '🔥',
        'hard':      '🔴',
        'critico':   '🎰',
    }
    LBL = {
        'soft': 'SOFT', 'warn_tip': 'WARN', 'close_tip': 'CLOSE',
        'high': 'HIGH', 'extreme': 'EXTREME', 'hard': 'HARD', 'critico': 'JACKPOT',
    }

    p_r = RANK.get(prev, 0)
    n_r = RANK.get(new_level, 0)

    if n_r <= 0:
        return None

    up       = n_r > p_r
    direction = '📈 UP' if up else '📉 DOWN'
    danger    = '⚠️' if up else 'ℹ️'

    prev_emoji = EMOJI.get(prev, '📊')
    new_emoji  = EMOJI.get(new_level, '📊')
    prev_lbl   = LBL.get(prev, prev.upper())
    new_lbl    = LBL.get(new_level, new_level.upper())

    # Funding: usa il rate salvato per il precedente, il corrente per il nuovo
    old_rate = prev_rate_saved if prev_rate_saved != 0.0 else prev_rate_pct
    sign_old = '+' if old_rate >= 0 else ''
    sign_new = '+' if rate_pct >= 0 else ''

    price_str = f"`${last_price:.6f}`" if last_price > 0 else "—"
    p24_arrow = "▲" if pct_24h >= 0 else "▼"
    p24_str   = f"{p24_arrow} `{('+' if pct_24h>=0 else '')}{pct_24h:.2f}%`" if pct_24h != 0 else ""
    price_line = f"💵 Price:  {price_str}  |  24h: {p24_str}\n" if last_price > 0 else ""

    return (
        f'{danger} *LEVEL CHANGE {direction}*\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'📌 *{symbol}*\n'
        f'{prev_emoji} `{prev_lbl}` → {new_emoji} `{new_lbl}`\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'📊 Prev. funding: `{sign_old}{old_rate:.4f}%`\n'
        f'📊 Curr. funding: `{sign_new}{rate_pct:.4f}%`\n'
        f'{price_line}'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'🔍 Check your open position!'
    )


# ══════════════════════════════════════════════════════════════════════════════
# LIQUIDAZIONE IMMINENTE
# ══════════════════════════════════════════════════════════════════════════════

_liq_alerted: dict[str, bool] = {}


def check_liquidation_risk(
    symbol: str,
    mark: float,
    liq: float,
    side: str,
    danger_pct: float = 15.0,
) -> str | None:
    if liq <= 0 or mark <= 0:
        return None

    dist = (mark - liq) / mark * 100 if side == 'Buy' else (liq - mark) / mark * 100
    if dist < 0:
        dist = 0.0

    if dist < danger_pct and not _liq_alerted.get(symbol):
        _liq_alerted[symbol] = True
        d = "LONG" if side == "Buy" else "SHORT"
        return (
            f'*LIQUIDATION IMMINENT -- {symbol}*\n'
            f'{d} position | Mark: `{mark:,.4f}`\n'
            f'Liq: `{liq:,.4f}` | Distanza: *{dist:.1f}%*\n'
            'ACT NOW!'
        )

    if dist >= 20.0 and _liq_alerted.get(symbol):
        _liq_alerted[symbol] = False

    return None


# ══════════════════════════════════════════════════════════════════════════════
# ALERT SIMULTANEI
# ══════════════════════════════════════════════════════════════════════════════

_last_multi_ts: float = 0.0


def check_multi_position_alert(funded_syms: list, min_count: int = 3) -> str | None:
    global _last_multi_ts
    count = len(funded_syms)
    if count < min_count:
        return None
    now = time.monotonic()
    if now - _last_multi_ts < 1800:
        return None
    _last_multi_ts = now
    sample = ', '.join(funded_syms[:5])
    dots   = '...' if count > 5 else ''
    return (
        f'{count} ALERT SIMULTANEI\n'
        f'{sample}{dots}\n'
        "Attenzione all'esposizione!"
    )
