"""
user_store.py — FundShot Bot
Gestione credenziali per-utente (multi-user support).
"""
import json, os, logging
from pathlib import Path

logger = logging.getLogger(__name__)
_BASE_DIR = Path(__file__).parent
USERS_FILE = _BASE_DIR / "users.json"
_store: dict = {}
_loaded = False

def _load():
    global _store, _loaded
    if _loaded:
        return
    if USERS_FILE.exists():
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                _store = json.load(f)
            logger.info("user_store: caricati %d utenti", len(_store))
        except Exception as e:
            logger.error("user_store errore lettura: %s", e)
            _store = {}
    else:
        _store = {}
    _loaded = True

def _save():
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(_store, f, indent=2)
    except Exception as e:
        logger.error("user_store errore scrittura: %s", e)

def get(chat_id) -> dict:
    _load()
    return dict(_store.get(str(chat_id), {}))

def set_key(chat_id, field: str, value):
    _load()
    cid = str(chat_id)
    if cid not in _store:
        _store[cid] = {}
    _store[cid][field] = value
    _save()

def get_api_key(chat_id) -> str:
    return get(chat_id).get("api_key", "")

def get_api_secret(chat_id) -> str:
    return get(chat_id).get("api_secret", "")

def has_credentials(chat_id) -> bool:
    _load()
    c = _store.get(str(chat_id), {})
    return bool(c.get("api_key")) and bool(c.get("api_secret"))

def all_users() -> list:
    _load()
    return list(_store.keys())

def users_with_credentials() -> list:
    _load()
    return [cid for cid, c in _store.items()
            if c.get("api_key") and c.get("api_secret")]

def remove_user(chat_id):
    _load()
    cid = str(chat_id)
    if cid in _store:
        del _store[cid]
        _save()

def migrate_from_env() -> bool:
    """Importa credenziali dal .env come primo utente."""
    _load()
    chat_id    = os.getenv("CHAT_ID", "")
    api_key    = os.getenv("BYBIT_API_KEY", "")
    api_secret = os.getenv("BYBIT_API_SECRET", "")
    if chat_id and api_key and api_secret:
        cid = str(chat_id)
        if cid not in _store:
            _store[cid] = {
                "api_key": api_key,
                "api_secret": api_secret,
                "testnet": os.getenv("TESTNET", "false").lower() == "true",
            }
            _save()
            logger.info("user_store: migrato %s dal .env", cid)
            return True
    return False

def delete(chat_id) -> bool:
    """Alias di remove_user - compatibilita con vecchio codice."""
    _load()
    cid = str(chat_id)
    if cid in _store:
        del _store[cid]
        _save()
        return True
    return False
