#!/usr/bin/env python3
"""
alert_config_manager.py — Gestione configurazione alert per Funding King Bot
Legge/scrive alert_config.json nella stessa directory del bot.
Usato da alert_logic.py e bot.py per decidere quali alert inviare.
"""
import json
import os
import threading

_LOCK = threading.Lock()
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert_config.json")

DEFAULT_CONFIG = {
    "enabled": {
        "critico":      True,
        "hard":         True,
        "extreme":      True,
        "high":         True,
        "close_tip":    True,
        "warn_tip":     False,
        "rientro":      True,
        "next_funding": True,
        "pump_dump":    False,
        "level_change": False,
        "liquidation":  True,
        "multi_pos":    False,
    },
    "thresholds": {
        "critico":   2.50,
        "hard":      2.00,
        "extreme":   1.50,
        "high":      1.00,
        "close_tip": 0.75,
        "warn_tip":  0.25,
        "rientro":   0.20,
    }
}

_config_cache = None
_config_mtime = 0.0


def _load_config() -> dict:
    """Carica la config dal file JSON (con cache basata su mtime)."""
    global _config_cache, _config_mtime
    with _LOCK:
        try:
            mtime = os.path.getmtime(_CONFIG_PATH)
            if _config_cache is not None and mtime == _config_mtime:
                return _config_cache
            with open(_CONFIG_PATH, "r") as f:
                data = json.load(f)
            # Merge con default per garantire chiavi mancanti
            config = {}
            config["enabled"] = {**DEFAULT_CONFIG["enabled"], **data.get("enabled", {})}
            config["thresholds"] = {**DEFAULT_CONFIG["thresholds"], **data.get("thresholds", {})}
            _config_cache = config
            _config_mtime = mtime
            return config
        except FileNotFoundError:
            # Prima volta: scrivi il default
            _save_config(DEFAULT_CONFIG)
            _config_cache = DEFAULT_CONFIG.copy()
            return DEFAULT_CONFIG
        except Exception:
            return DEFAULT_CONFIG


def _save_config(config: dict):
    """Salva la config su file JSON."""
    with open(_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def get_config() -> dict:
    """Ritorna la configurazione corrente (con cache)."""
    return _load_config()


def is_enabled(alert_type: str) -> bool:
    """
    Controlla se un tipo di alert è abilitato.
    alert_type: 'critico'|'hard'|'extreme'|'high'|'close_tip'|'warn_tip'|
                'rientro'|'next_funding'|'pump_dump'|'level_change'|'liquidation'|'multi_pos'
    Default True per tipi sconosciuti (fail-open).
    """
    try:
        cfg = _load_config()
        return cfg["enabled"].get(alert_type, True)
    except Exception:
        return True


def get_threshold(alert_type: str, default: float = None) -> float:
    """
    Ritorna la soglia configurata per un tipo di alert.
    Se non trovata, ritorna default (o il valore di DEFAULT_CONFIG).
    """
    try:
        cfg = _load_config()
        val = cfg["thresholds"].get(alert_type)
        if val is not None:
            return float(val)
    except Exception:
        pass
    if default is not None:
        return default
    return DEFAULT_CONFIG["thresholds"].get(alert_type, 0.0)


def update_config(new_config: dict) -> dict:
    """
    Aggiorna parzialmente la configurazione.
    new_config può contenere {"enabled": {...}, "thresholds": {...}} (parziale).
    Ritorna la configurazione aggiornata.
    """
    with _LOCK:
        current = _load_config()
        if "enabled" in new_config:
            current["enabled"].update(new_config["enabled"])
        if "thresholds" in new_config:
            for k, v in new_config["thresholds"].items():
                try:
                    current["thresholds"][k] = float(v)
                except (ValueError, TypeError):
                    pass
        _save_config(current)
        global _config_cache, _config_mtime
        _config_cache = current
        try:
            _config_mtime = os.path.getmtime(_CONFIG_PATH)
        except Exception:
            _config_mtime = 0.0
        return current


def reset_to_defaults() -> dict:
    """Ripristina la configurazione di default e salva su file."""
    with _LOCK:
        import copy
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        _save_config(cfg)
        global _config_cache, _config_mtime
        _config_cache = cfg
        try:
            _config_mtime = os.path.getmtime(_CONFIG_PATH)
        except Exception:
            _config_mtime = 0.0
        return cfg


if __name__ == "__main__":
    # Test rapido
    print("Config attuale:", json.dumps(get_config(), indent=2))
    print("critico abilitato:", is_enabled("critico"))
    print("pump_dump abilitato:", is_enabled("pump_dump"))
    print("Soglia high:", get_threshold("high"))
