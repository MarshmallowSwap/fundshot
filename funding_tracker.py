"""
funding_tracker.py — Registra il guadagno/costo da funding per ogni ciclo.

Per ogni simbolo che ha ricevuto un alert HIGH/EXTREME/HARD e aveva una
posizione aperta, al momento del reset (funding pagato) viene calcolato:

  gain = size × mark_price × rate_pct/100
  segno: +  se position SHORT e rate > 0  (ricevi funding dai longs)
          +  se position LONG  e rate < 0  (ricevi funding dagli shorts)
          -  altrimenti (stai pagando)

I dati vengono persistiti in funding_gains.json nella cartella del bot.
"""

import json
import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

TZ_IT      = ZoneInfo("Europe/Rome")
GAINS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "funding_gains.json")

# Struttura in memoria:
# {
#   "SAHARAUSDT": {
#       "cycles": [
#           {
#               "ts":         "27/02/2026 12:02 CET",
#               "rate_pct":   2.45,
#               "mark_price": 0.0012,
#               "size":       10000,
#               "side":       "Sell",
#               "gain_usdt":  29.40,
#               "level":      "hard"
#           }, ...
#       ],
#       "total_gain_usdt": 58.80,
#       "last_gain_usdt":  29.40,
#       "last_rate_pct":   2.45,
#       "last_ts":         "27/02/2026 20:02 CET",
#       "position_open_ts": "27/02/2026 10:00 CET"   # primo ciclo registrato
#   }
# }
_data: dict[str, dict] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Persistenza
# ─────────────────────────────────────────────────────────────────────────────

def load() -> None:
    """Carica i dati da disco al boot del bot."""
    global _data
    if os.path.exists(GAINS_FILE):
        try:
            with open(GAINS_FILE, "r", encoding="utf-8") as f:
                _data = json.load(f)
            logger.info("funding_tracker: caricati dati per %d simboli da %s",
                        len(_data), GAINS_FILE)
        except Exception as e:
            logger.warning("funding_tracker: errore caricamento %s: %s", GAINS_FILE, e)
            _data = {}
    else:
        _data = {}


def save() -> None:
    """Salva i dati su disco."""
    try:
        with open(GAINS_FILE, "w", encoding="utf-8") as f:
            json.dump(_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("funding_tracker: errore salvataggio: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Calcolo guadagno
# ─────────────────────────────────────────────────────────────────────────────

def calc_gain(size: float, mark_price: float, rate_pct: float, side: str) -> float:
    """
    Calcola il guadagno/costo netto in USDT per un ciclo di funding.

    side: "Buy" (LONG) | "Sell" (SHORT)
    rate_pct: in percentuale (es. 2.45 = 2.45%)

    Logica Bybit perpetual:
      - rate > 0 → longs pagano shorts  → SHORT guadagna, LONG paga
      - rate < 0 → shorts pagano longs → LONG guadagna, SHORT paga
    """
    position_value = size * mark_price
    payment = position_value * (rate_pct / 100.0)
    # SHORT riceve quando rate > 0, paga quando rate < 0
    if side == "Sell":   # SHORT
        return payment
    else:                # LONG (Buy)
        return -payment


# ─────────────────────────────────────────────────────────────────────────────
# API pubblica
# ─────────────────────────────────────────────────────────────────────────────

def record_cycle(
    symbol: str,
    rate_pct: float,
    mark_price: float,
    size: float,
    side: str,
    level: str,
) -> float:
    """
    Registra un ciclo di funding completato e restituisce il guadagno calcolato.
    Chiamare quando rate_pct passa da HIGH+ a quasi-zero (funding pagato).
    """
    gain = calc_gain(size, mark_price, rate_pct, side)
    now_str = datetime.now(TZ_IT).strftime("%d/%m/%Y %H:%M %Z")

    entry = {
        "ts":         now_str,
        "rate_pct":   round(rate_pct, 4),
        "mark_price": mark_price,
        "size":       size,
        "side":       side,
        "gain_usdt":  round(gain, 4),
        "level":      level,
    }

    if symbol not in _data:
        _data[symbol] = {
            "cycles":           [],
            "total_gain_usdt":  0.0,
            "last_gain_usdt":   0.0,
            "last_rate_pct":    0.0,
            "last_ts":          "",
            "position_open_ts": now_str,
        }

    sym = _data[symbol]
    sym["cycles"].append(entry)
    sym["total_gain_usdt"] = round(sym["total_gain_usdt"] + gain, 4)
    sym["last_gain_usdt"]  = round(gain, 4)
    sym["last_rate_pct"]   = round(rate_pct, 4)
    sym["last_ts"]         = now_str

    # Mantieni max 50 cicli per simbolo
    if len(sym["cycles"]) > 50:
        sym["cycles"] = sym["cycles"][-50:]

    save()
    logger.info(
        "funding_tracker: %s | rate=%.4f%% | side=%s | gain=%.4f USDT | totale=%.4f USDT",
        symbol, rate_pct, side, gain, sym["total_gain_usdt"]
    )
    return gain


def get_data(symbol: str) -> dict | None:
    """Restituisce i dati di un simbolo, o None se non ci sono cicli registrati."""
    return _data.get(symbol)


def get_all_symbols() -> list[str]:
    """Restituisce tutti i simboli con almeno un ciclo registrato."""
    return list(_data.keys())


def reset_symbol(symbol: str) -> None:
    """
    Azzera i dati di un simbolo (es. quando la posizione viene chiusa).
    I dati storici vengono spostati in un campo 'archived'.
    """
    if symbol in _data:
        # Archivia prima di cancellare
        archived = _data[symbol].copy()
        archived["archived_ts"] = datetime.now(TZ_IT).strftime("%d/%m/%Y %H:%M %Z")
        _data[f"{symbol}_archived_{archived['archived_ts'][:10]}"] = archived
        del _data[symbol]
        save()


def format_summary(positions: list[dict] | None = None) -> str:
    """
    Formatta il riepilogo guadagni funding per Telegram.

    Se viene passata la lista delle posizioni aperte (da bc.get_positions()),
    mostra anche le posizioni senza cicli registrati (totale = 0).
    """
    from zoneinfo import ZoneInfo as _ZI
    _tz = _ZI("Europe/Rome")

    # Simboli da mostrare: quelli con cicli + posizioni aperte senza cicli
    symbols_with_cycles = set(_data.keys())
    open_symbols: dict[str, dict] = {}
    if positions:
        for p in positions:
            s = p["symbol"]
            open_symbols[s] = p

    all_symbols = sorted(
        symbols_with_cycles | set(open_symbols.keys()),
        key=lambda s: abs(_data[s]["total_gain_usdt"]) if s in _data else 0,
        reverse=True,
    )

    if not all_symbols:
        return (
            "📊 *PROFITTO FUNDING*\n\n"
            "Nessun ciclo di funding registrato.\n\n"
            "ℹ️ I dati vengono registrati automaticamente quando:\n"
            "1️⃣ Il bot invia un alert HIGH/EXTREME/HARD\n"
            "2️⃣ Hai una posizione aperta sul simbolo\n"
            "3️⃣ Il funding rate torna a zero (ciclo completato)"
        )

    lines = ["📊 *PROFITTO FUNDING*\n"]
    grand_total = 0.0

    for sym in all_symbols:
        d = _data.get(sym)
        pos = open_symbols.get(sym)

        # Icona livello
        if d and d["cycles"]:
            last_level = d["cycles"][-1].get("level", "high")
            level_icon = {"hard": "🔴", "extreme": "🔥", "high": "🚨"}.get(last_level, "📌")
        else:
            level_icon = "📌"

        side_label = ""
        if pos:
            side_label = "SHORT" if pos["side"] == "Sell" else "LONG"
            side_icon  = "🔻" if pos["side"] == "Sell" else "🔺"
        elif d and d["cycles"]:
            last = d["cycles"][-1]
            side_label = "SHORT" if last["side"] == "Sell" else "LONG"
            side_icon  = "🔻" if last["side"] == "Sell" else "🔺"
        else:
            side_icon = "📍"

        lines.append(f"{level_icon} *{sym}* {side_icon} {side_label}")

        if d:
            n_cycles    = len(d["cycles"])
            last_gain   = d["last_gain_usdt"]
            total_gain  = d["total_gain_usdt"]
            last_rate   = d["last_rate_pct"]
            last_ts     = d["last_ts"]
            open_ts     = d.get("position_open_ts", "—")
            grand_total += total_gain

            gain_emoji  = "💰" if last_gain >= 0 else "💸"
            total_emoji = "💰" if total_gain >= 0 else "💸"
            sign_last   = "+" if last_gain >= 0 else ""
            sign_total  = "+" if total_gain >= 0 else ""

            lines.append(f"  ├─ Rate ultimo ciclo: {'+' if last_rate>=0 else ''}{last_rate:.4f}%")
            lines.append(f"  ├─ {gain_emoji} Guadagno ultimo ciclo: `{sign_last}{last_gain:.4f} USDT`")
            lines.append(f"  ├─ {total_emoji} Totale da apertura: `{sign_total}{total_gain:.4f} USDT`")
            lines.append(f"  ├─ Cicli registrati: {n_cycles}")
            lines.append(f"  └─ Ultimo aggiornamento: {last_ts}")
        else:
            lines.append("  └─ Nessun ciclo registrato (posizione aperta senza alert HIGH+)")

        lines.append("")

    # Totale complessivo
    sign = "+" if grand_total >= 0 else ""
    total_icon = "✅" if grand_total >= 0 else "⚠️"
    lines.append(f"{'─'*28}")
    lines.append(f"{total_icon} *TOTALE COMPLESSIVO: `{sign}{grand_total:.4f} USDT`*")

    return "\n".join(lines)
