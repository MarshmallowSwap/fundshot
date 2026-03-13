"""
watchlist_manager.py — FundShot Bot
Gestione persistente della watchlist, simboli silenziati e soglie custom per simbolo.
I dati sono salvati in watchlist.json nella stessa directory del bot.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Percorso file di persistenza ──────────────────────────────────────────────
_DATA_FILE = Path(os.getenv("WATCHLIST_FILE", "watchlist.json"))

# ── Struttura dati in memoria ─────────────────────────────────────────────────
# {
#   "watchlist": ["BTCUSDT", "ETHUSDT", ...],   # lista simboli monitorati (vuota = tutti)
#   "muted":     ["SOLUSDT", ...],               # simboli silenziati
#   "custom_thresholds": {                       # soglie custom per simbolo
#       "PEPEUSDT": {
#           "hard": 3.0, "extreme": 2.0, "high": 1.5,
#           "close_tip": 0.5, "rientro": 1.2
#       }
#   }
# }
_data: dict = {
    "watchlist": [],
    "muted": [],
    "custom_thresholds": {},
}


# ══════════════════════════════════════════════════════════════════════════════
# Persistenza
# ══════════════════════════════════════════════════════════════════════════════

def load():
    """Carica la watchlist dal file JSON. Crea il file se non esiste."""
    global _data
    if _DATA_FILE.exists():
        try:
            with open(_DATA_FILE, "r") as f:
                loaded = json.load(f)
            # Merge sicuro: mantieni struttura anche con file vecchi
            _data["watchlist"]         = loaded.get("watchlist", [])
            _data["muted"]             = loaded.get("muted", [])
            _data["custom_thresholds"] = loaded.get("custom_thresholds", {})
            logger.info(
                "Watchlist caricata: %d simboli, %d silenziati, %d custom",
                len(_data["watchlist"]), len(_data["muted"]),
                len(_data["custom_thresholds"]),
            )
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Errore lettura watchlist.json: %s — uso defaults", e)
    else:
        save()
        logger.info("watchlist.json creato con valori default.")


def save():
    """Salva lo stato corrente su file JSON."""
    try:
        with open(_DATA_FILE, "w") as f:
            json.dump(_data, f, indent=2, ensure_ascii=False)
    except OSError as e:
        logger.error("Errore salvataggio watchlist.json: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# Watchlist
# ══════════════════════════════════════════════════════════════════════════════

def get_watchlist() -> list[str]:
    """Restituisce la lista dei simboli monitorati (vuota = tutti)."""
    return list(_data["watchlist"])


def add_symbols(symbols: list[str]) -> list[str]:
    """Aggiunge simboli alla watchlist. Restituisce quelli effettivamente aggiunti."""
    added = []
    for s in symbols:
        s = s.upper()
        if s not in _data["watchlist"]:
            _data["watchlist"].append(s)
            added.append(s)
    if added:
        save()
    return added


def remove_symbols(symbols: list[str]) -> list[str]:
    """Rimuove simboli dalla watchlist. Restituisce quelli effettivamente rimossi."""
    removed = []
    for s in symbols:
        s = s.upper()
        if s in _data["watchlist"]:
            _data["watchlist"].remove(s)
            removed.append(s)
    if removed:
        save()
    return removed


def clear_watchlist():
    """Svuota la watchlist (torna a monitorare tutti)."""
    _data["watchlist"] = []
    save()


# ══════════════════════════════════════════════════════════════════════════════
# Mute
# ══════════════════════════════════════════════════════════════════════════════

def get_muted() -> list[str]:
    return list(_data["muted"])


def mute_symbols(symbols: list[str]) -> list[str]:
    added = []
    for s in symbols:
        s = s.upper()
        if s not in _data["muted"]:
            _data["muted"].append(s)
            added.append(s)
    if added:
        save()
    return added


def unmute_symbols(symbols: list[str]) -> list[str]:
    removed = []
    for s in symbols:
        s = s.upper()
        if s in _data["muted"]:
            _data["muted"].remove(s)
            removed.append(s)
    if removed:
        save()
    return removed


# ══════════════════════════════════════════════════════════════════════════════
# Filtro principale (usato in bot.py e commands.py)
# ══════════════════════════════════════════════════════════════════════════════

def is_watched(symbol: str) -> bool:
    """
    Restituisce True se il simbolo deve essere monitorato.
    Logica:
      1. Se è silenziato → False
      2. Se watchlist è vuota → True (monitora tutto)
      3. Se watchlist non vuota → True solo se il simbolo è nella lista
    """
    s = symbol.upper()
    if s in _data["muted"]:
        return False
    if not _data["watchlist"]:
        return True
    return s in _data["watchlist"]


def is_explicitly_watched(symbol: str) -> bool:
    """
    Restituisce True SOLO se il simbolo è nella watchlist esplicita (non-vuota).
    A differenza di is_watched, NON restituisce True in modalità 'monitora tutto'.
    Usato per decidere se inviare alert secondari (close_tip, pump/dump)
    a simboli che non hanno ancora ricevuto un alert di funding.
    """
    s = symbol.upper()
    if s in _data["muted"]:
        return False
    return bool(_data["watchlist"]) and s in _data["watchlist"]


# ══════════════════════════════════════════════════════════════════════════════
# Soglie custom per simbolo
# ══════════════════════════════════════════════════════════════════════════════

_VALID_LEVELS = {"hard", "extreme", "high", "close_tip", "rientro"}

_DEFAULT_THRESHOLDS = {
    "hard":      2.00,
    "extreme":   1.50,
    "high":      1.00,
    "close_tip": 0.23,
    "rientro":   0.75,
}


def get_custom_threshold(symbol: str, level: str) -> Optional[float]:
    """
    Restituisce la soglia custom per il simbolo e livello, o None se non impostata.
    """
    return _data["custom_thresholds"].get(symbol.upper(), {}).get(level)


def set_custom_threshold(symbol: str, level: str, value: float) -> bool:
    """
    Imposta una soglia custom per il simbolo.
    Restituisce False se il livello non è valido.
    """
    if level not in _VALID_LEVELS:
        return False
    s = symbol.upper()
    if s not in _data["custom_thresholds"]:
        _data["custom_thresholds"][s] = {}
    _data["custom_thresholds"][s][level] = round(value, 4)
    save()
    return True


def remove_custom_thresholds(symbol: str):
    """Rimuove tutte le soglie custom per il simbolo (torna ai default)."""
    s = symbol.upper()
    if s in _data["custom_thresholds"]:
        del _data["custom_thresholds"][s]
        save()


def get_all_custom_thresholds() -> dict:
    return dict(_data["custom_thresholds"])


def get_effective_threshold_for_symbol(symbol: str, level: str) -> float:
    """
    Restituisce la soglia effettiva per il simbolo:
    custom > default globale.
    (La logica ibrida dinamica è gestita da alert_logic.py separatamente)
    """
    custom = get_custom_threshold(symbol, level)
    if custom is not None:
        return custom
    return _DEFAULT_THRESHOLDS.get(level, 1.0)


# ══════════════════════════════════════════════════════════════════════════════
# Utility
# ══════════════════════════════════════════════════════════════════════════════

def get_summary() -> dict:
    """Riepilogo completo dello stato watchlist."""
    return {
        "watchlist":         get_watchlist(),
        "muted":             get_muted(),
        "custom_thresholds": get_all_custom_thresholds(),
        "mode":              "filtro attivo" if _data["watchlist"] else "monitora tutto",
    }


def validate_symbols(symbols: list[str], known_symbols: set[str]) -> tuple[list[str], list[str]]:
    """
    Valida i simboli contro quelli noti da Bybit.
    Restituisce (validi, non_trovati).
    """
    valid, unknown = [], []
    for s in symbols:
        s = s.upper()
        if not s.endswith("USDT"):
            s = s + "USDT"
        if s in known_symbols:
            valid.append(s)
        else:
            unknown.append(s)
    return valid, unknown
