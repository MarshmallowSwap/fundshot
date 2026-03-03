"""
session_manager.py — Funding King Bot
Sessioni Bybit HTTP per-utente.
"""
import logging
import user_store
from pybit.unified_trading import HTTP

logger = logging.getLogger(__name__)
_sessions: dict = {}

def get_session(chat_id) -> HTTP:
    """Restituisce la sessione per l'utente, creandola se non esiste."""
    cid = str(chat_id)
    if cid in _sessions:
        return _sessions[cid]
    api_key    = user_store.get_api_key(chat_id)
    api_secret = user_store.get_api_secret(chat_id)
    testnet    = user_store.get(chat_id).get("testnet", False)
    if api_key and api_secret:
        s = HTTP(testnet=testnet, api_key=api_key, api_secret=api_secret)
        logger.info("session_manager: sessione autenticata creata per %s", cid)
    else:
        s = HTTP(testnet=testnet)
        logger.info("session_manager: sessione anonima creata per %s", cid)
    _sessions[cid] = s
    return s

def reload_session(chat_id) -> HTTP:
    """Forza la ricreazione della sessione (dopo cambio credenziali)."""
    cid = str(chat_id)
    _sessions.pop(cid, None)
    return get_session(chat_id)

def remove_session(chat_id):
    """Rimuove la sessione per un utente."""
    _sessions.pop(str(chat_id), None)

def has_session(chat_id) -> bool:
    return str(chat_id) in _sessions
