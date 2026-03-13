#!/usr/bin/env python3
"""
proxy_v5.py Ã¢ÂÂ FundShot Proxy
Aggiunge tutti gli endpoint mancanti al proxy v4 esistente.
Copia questo file sul server e riavvia con:
    pkill -f proxy && python3 proxy_v5.py &

Endpoint NUOVI rispetto a v4:
  GET/POST  /api/config        Ã¢ÂÂ API key / secret / testnet
  GET/POST  /api/mode          Ã¢ÂÂ modalitÃÂ  alert/trade
  GET/POST  /api/risk-params   Ã¢ÂÂ parametri rischio
  GET/POST  /api/thresholds    Ã¢ÂÂ soglie FR
  GET/POST  /api/interval      Ã¢ÂÂ intervallo refresh
  GET       /api/watchlist     Ã¢ÂÂ lista simboli watchlist
  POST      /api/watchlist     Ã¢ÂÂ aggiungi simbolo
  DELETE    /api/watchlist     Ã¢ÂÂ rimuovi simbolo
  POST      /api/close-all     Ã¢ÂÂ chiudi tutte le posizioni
  GET       /api/stats         Ã¢ÂÂ statistiche aggregate
  GET       /api/logs          Ã¢ÂÂ ultimi log del bot

Endpoint giÃÂ  presenti in v4 (mantenuti identici):
  GET  /api/status
  GET  /api/tickers
  GET  /api/positions
  GET  /api/wallet
  GET  /api/alert-config
  POST /api/alert-config
  GET  /api/close-by-mm
  GET  /api/close-by-pnl
"""

import json, os, time, threading, logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
#  CONFIG
# Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
PORT        = 8080
CONFIG_FILE = os.path.expanduser('~/.fundshot_config.json')
STATE_FILE  = os.path.expanduser('~/.fundshot_state.json')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('proxy_v5')

# Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
#  PERSISTENT CONFIG  (legge/scrive su disco)
# Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
DEFAULT_CONFIG = {
    "api_key":    "",
    "api_secret": "",
    "testnet":    False,
    "mode":       "alert",
    "interval":   60,
    "watchlist":  [],
    "risk_params": {
        "mmr_threshold": 5.0,
        "profit_target": 1.0,
        "max_loss":       2.0,
        "leverage":       10,
        "size_usdt":      100,
        "take_profit_pct": 1.0,
        "stop_loss_pct":   2.0,
        "max_positions":   5,
        "max_exposure_pct": 50.0
    },
    "thresholds": {
        "critico": 2.5, "hard": 2.0, "extreme": 1.5,
        "high": 1.0, "close": 0.75, "warn": 0.25, "rientro": 0.20
    },
    "alert_config": {
        "enabled": {
            "critico": True, "hard": True, "extreme": True,
            "high": True, "close_tip": True, "warn_tip": False,
            "rientro": True, "next_funding": True,
            "pump_dump": False, "level_change": False,
            "liquidation": True, "multi_pos": False
        },
        "thresholds": {
            "critico": 2.5, "hard": 2.0, "extreme": 1.5,
            "high": 1.0, "close_tip": 0.75, "warn_tip": 0.25, "rientro": 0.2
        }
    }
}

_cfg_lock = threading.Lock()

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                data = json.load(f)
            # Deep merge with defaults
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            for k, v in data.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
            return cfg
        except Exception as e:
            log.warning(f"Config load error: {e}")
    return json.loads(json.dumps(DEFAULT_CONFIG))

def save_config(cfg):
    with _cfg_lock:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(cfg, f, indent=2)

# Carica config al boot
_config = load_config()
_config['mode'] = 'alert'  # HARDCODED: always alert-only mode
log.info(f"Config loaded Ã¢ÂÂ api_key={'SET' if _config.get('api_key') else 'EMPTY'}")

# Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
#  BYBIT API HELPER  (usa le chiavi dal config)
# Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
def bybit_base():
    return 'https://api-demo.bybit.com' if _config.get('demo') else 'https://api.bybit.com'

def bybit_get(path, params=None):
    """Chiama Bybit API (senza auth per endpoint pubblici)."""
    url = bybit_base() + path
    if params:
        url += '?' + '&'.join(f'{k}={v}' for k,v in params.items())
    req = Request(url, headers={'User-Agent': 'FundShot/5.0'})
    with urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def bybit_get_auth(path, params=None):
    """Chiama Bybit API privata con HMAC v5."""
    import hmac as _h, hashlib as _hs
    key = _config.get("api_key", "")
    secret = _config.get("api_secret", "")
    if not key or not secret:
        return {"retCode": -1, "retMsg": "no_key"}
    params = params or {}
    ts = str(int(__import__("time").time() * 1000))
    rw = "5000"
    ps = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sign_payload = ts + key + rw + ps
    sig = _h.new(secret.encode(), sign_payload.encode(), _hs.sha256).hexdigest()
    from urllib.request import Request as _R, urlopen as _u
    import json as _j
    url = bybit_base() + path + ("?" + ps if ps else "")
    hdrs = {
        "X-BAPI-API-KEY": key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-SIGN": sig,
        "X-BAPI-RECV-WINDOW": rw,
        "Content-Type": "application/json",
    }
    req = _R(url, headers=hdrs)
    with _u(req, timeout=10) as r:
        return _j.loads(r.read())



# Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
#  CACHE  (tickers, positions, wallet)
# Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
_cache = {'tickers': None, 'positions': None, 'wallet': None, 'ts': {}}
_cache_lock = threading.Lock()
CACHE_TTL = 30  # secondi (tickers)
CACHE_TTL_POS = 5   # secondi (posizioni e wallet — aggiornamento rapido PnL)

def cache_get(key):
    with _cache_lock:
        ttl = CACHE_TTL_POS if key in ('positions','wallet') else CACHE_TTL
        if _cache[key] and (time.time() - _cache['ts'].get(key, 0)) < ttl:
            return _cache[key]
    return None

def cache_set(key, value):
    with _cache_lock:
        _cache[key] = value
        _cache['ts'][key] = time.time()

# Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
#  REQUEST HANDLER
# Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
class ProxyHandler(BaseHTTPRequestHandler):

    def do_POST(self):
        if self.path == "/api/config":
            self._handle_config_update()
            return
        self.send_error(404, "Not Found")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _handle_config_update(self):
        import json, os, signal
        CONFIG_PATH = os.path.expanduser("~/.fundshot_config.json")
        try:
            length  = int(self.headers.get("Content-Length", 0))
            raw     = self.rfile.read(length)
            payload = json.loads(raw)
        except Exception as e:
            self._json_response(400, {"ok": False, "error": str(e)}); return
        try:
            with open(CONFIG_PATH) as f:
                existing = json.load(f)
        except Exception:
            existing = {}
        existing.update(payload)
        existing["_updated_from_dashboard"] = True
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            self._json_response(500, {"ok": False, "error": str(e)}); return
        reloaded = False
        try:
            import subprocess
            subprocess.Popen(["systemctl", "reload-or-restart", "fundshot"])
            reloaded = True
        except Exception:
            pass
        self._json_response(200, {"ok": True, "config_path": CONFIG_PATH, "reloaded": reloaded})

    def _json_response(self, code, data):
        import json
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


    def log_message(self, fmt, *args):
        log.info(f"{self.client_address[0]} Ã¢ÂÂ {fmt % args}")

    def _headers(self, code=200, ctype='application/json'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self._headers(code)
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def _not_found(self):
        self._json({'ok': False, 'msg': 'not found'}, 200)  # 200 per compat con v4

    # Ã¢ÂÂÃ¢ÂÂ OPTIONS (CORS preflight) Ã¢ÂÂÃ¢ÂÂ
    def do_OPTIONS(self):
        self._headers(204)

    # Ã¢ÂÂÃ¢ÂÂ GET Ã¢ÂÂÃ¢ÂÂ
    def do_GET(self):
        p = urlparse(self.path).path.rstrip('/')

        if p == '/api/status':
            self._json({
                'ok': True,
                'msg': 'proxy running v5',
                'key_set': bool(_config.get('api_key')),
                'mode': _config.get('mode', 'alert'),
                'testnet': _config.get('testnet', False),
                'version': '5.0'
            })

        elif p == '/api/config':
            key = _config.get('api_key', '')
            sec = _config.get('api_secret', '')
            self._json({
                'ok': True,
                'api_key': key[:8] + 'Ã¢ÂÂ¦' + key[-4:] if len(key) > 12 else ('SET' if key else ''),
                'api_secret_masked': 'Ã¢ÂÂ¢' * min(len(sec), 32),
                'testnet': _config.get('testnet', False)
            })

        elif p == '/api/mode':
            self._json({'ok': True, 'mode': _config.get('mode', 'alert')})

        elif p == '/api/risk-params':
            self._json({'ok': True, **_config.get('risk_params', {})})

        elif p == '/api/thresholds':
            self._json({'ok': True, **_config.get('thresholds', {})})

        elif p == '/api/interval':
            self._json({'ok': True, 'interval': _config.get('interval', 60)})

        elif p == '/api/watchlist':
            self._json({'ok': True, 'watchlist': _config.get('watchlist', [])})

        elif p == '/api/alert-config':
            self._json({'ok': True, 'config': _config.get('alert_config', {})})

        elif p == '/api/tickers':
            cached = cache_get('tickers')
            if cached:
                self._json(cached)
                return
            try:
                data = bybit_get('/v5/market/tickers', {'category': 'linear', 'limit': '1000'})
                tickers = {}
                for t in data.get('result', {}).get('list', []):
                    sym = t.get('symbol', '')
                    if not sym.endswith('USDT'): continue
                    tickers[sym] = {
                        'symbol':          sym,
                        'fundingRate':     float(t.get('fundingRate', 0)),
                        'nextFundingTime': int(t.get('nextFundingTime', 0)),
                        'markPrice':       float(t.get('markPrice', 0)),
                        'indexPrice':      float(t.get('indexPrice', 0)),
                        'price24hPcnt':    float(t.get('price24hPcnt', 0)),
                        'turnover24h':     float(t.get('turnover24h', 0)),
                        'volume24h':       float(t.get('volume24h', 0)),
                    }
                result = {'ok': True, 'count': len(tickers), 'tickers': list(tickers.values())}
                cache_set('tickers', result)
                self._json(result)
            except Exception as e:
                self._json({'ok': False, 'msg': str(e)}, 500)

        elif p == '/api/monitoring':
            try:
                import os as _os, json as _json
                mon_file = '/tmp/fs_monitoring.json'
                data = _json.load(open(mon_file)) if _os.path.exists(mon_file) else {}
                self._json({'ok': True, 'monitoring': data})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)})
            return

        elif p == '/api/results':
            import json as _jj
            try:
                with open('/tmp/fs_results.json') as f:
                    data = _jj.load(f)
            except Exception:
                data = []
            self._json({"ok": True, "results": data})

        elif p == '/api/closed-pnl':
            # Legge closed PnL direttamente da Bybit (ultimi 50 trade)
            k = _config.get("api_key","")
            s = _config.get("api_secret","")
            if not k:
                self._json({"ok": False, "msg": "no_key", "trades": []})
            else:
                try:
                    import time as _t, hmac as _h, hashlib as _hs, urllib.request as _u, json as _jj2
                    recv_window = "5000"
                    ts = str(int(_t.time()*1000))
                    params = "category=linear&limit=50"
                    # Firma corretta Bybit V5: ts + apiKey + recvWindow + queryString
                    sign_str = ts + k + recv_window + params
                    sig = _h.new(s.encode(), sign_str.encode(), _hs.sha256).hexdigest()
                    # Demo account usa endpoint diverso
                    base = "https://api-demo.bybit.com" if _config.get("demo") else "https://api.bybit.com"
                    url = f"{base}/v5/position/closed-pnl?{params}"
                    req = _u.Request(url, headers={
                        "X-BAPI-API-KEY":     k,
                        "X-BAPI-TIMESTAMP":   ts,
                        "X-BAPI-RECV-WINDOW": recv_window,
                        "X-BAPI-SIGN":        sig,
                    })
                    with _u.urlopen(req, timeout=8) as resp:
                        raw = _jj2.loads(resp.read())
                    items = raw.get("result",{}).get("list",[])
                    trades = [{
                        "symbol":    x.get("symbol",""),
                        "side":      "LONG" if x.get("side")=="Buy" else "SHORT",
                        "qty":       x.get("qty",""),
                        "entry":     x.get("avgEntryPrice",""),
                        "exit":      x.get("avgExitPrice",""),
                        "pnl_usdt":  float(x.get("closedPnl","0")),
                        "ts":        str(int(x.get("updatedTime","0"))//1000),
                        "source":    "bybit",
                    } for x in items]
                    self._json({"ok": True, "trades": trades, "debug": raw.get("retMsg","")})
                except Exception as e:
                    self._json({"ok": False, "error": str(e), "trades": []})

        elif p == '/api/oi':
            try:
                import os as _os, json as _json
                oi_file = '/tmp/fs_oi.json'
                data = _json.load(open(oi_file)) if _os.path.exists(oi_file) else {}
                self._json({'ok': True, 'oi': data})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)})
            return
        elif p == '/api/positions':
            k=_config.get("api_key","")
            if not k: self._json({"ok":True,"positions":[],"msg":"no_key"})
            else:
                cp=cache_get("positions")
                if cp: self._json(cp)
                else:
                    try:
                        d=bybit_get_auth("/v5/position/list",{"category":"linear","settleCoin":"USDT"})
                        items=d.get("result",{}).get("list",[])
                        pos=[{"symbol":x["symbol"],"side":x["side"],"size":x["size"],"avgPrice":x["avgPrice"],"markPrice":x["markPrice"],"liqPrice":x["liqPrice"],"unrealisedPnl":x["unrealisedPnl"],"cumRealisedPnl":x["cumRealisedPnl"],"leverage":x["leverage"],"positionValue":x["positionValue"]} for x in items if float(x.get("size",0))!=0]
                        res={"ok":True,"positions":pos}
                        cache_set("positions",res); self._json(res)
                    except Exception as e: self._json({"ok":False,"msg":str(e)})

        elif p == '/api/wallet':
            k=_config.get("api_key","")
            if not k: self._json({"ok":False,"msg":"no_key"})
            else:
                cw=cache_get("wallet")
                if cw: self._json(cw)
                else:
                    try:
                        d=bybit_get_auth("/v5/account/wallet-balance",{"accountType":"UNIFIED"})
                        a=d.get("result",{}).get("list",[{}])[0]
                        res={"ok":True,"equity":a.get("totalEquity","0"),"available":a.get("totalAvailableBalance","0"),"unrealisedPnl":a.get("totalUnrealisedPnl","0"),"margin":a.get("totalInitialMargin","0"),"walletBal":a.get("totalWalletBalance","0"),"realisedPnl":a.get("totalPerpRPL","0")}
                        cache_set("wallet",res); self._json(res)
                    except Exception as e: self._json({"ok":False,"msg":str(e)})

        elif p == '/api/close-by-mm':
            # DISABLED: trading disabled
            self._json({'ok': False, 'msg': 'Trading disabilitato'}); return
            self._json({'ok': False, 'msg': 'unavail'})

        elif p == '/api/close-by-pnl':
            # DISABLED: trading disabled
            self._json({'ok': False, 'msg': 'Trading disabilitato'}); return
            self._json({'ok': False, 'msg': 'unavail'})

        elif p == '/api/stats':
            cached_t = cache_get('tickers')
            tickers = (cached_t.get('tickers', []) if cached_t else [])
            frs = [abs(float(t.get('fundingRate', 0)) * 100) for t in tickers]
            self._json({
                'ok': True,
                'total_symbols': len(tickers),
                'avg_fr': round(sum(frs)/len(frs), 4) if frs else 0,
                'max_fr': round(max(frs), 4) if frs else 0,
                'mode': _config.get('mode', 'alert'),
                'uptime': round(time.time() - _start_time, 0),
            })

        elif p == '/api/logs':
            self._json({'ok': True, 'logs': [], 'msg': 'Log streaming not implemented in v5'})

        elif p == '/api/bot-status':
            running, pid = _bot_is_running()
            self._json({'ok': True, 'running': running, 'pid': pid or None})

        elif p == '/api/bot-log':
            logs = _read_log(BOT_LOG, 80)
            self._json({'ok': True, 'logs': logs, 'path': BOT_LOG})

        elif p == '/api/bot-altlog':
            # cerca log alternativi
            import glob
            for pat in [BOT_DIR+'/bot.log', BOT_DIR+'/*.log', '/var/log/proxy_v5.log']:
                for f in sorted(glob.glob(pat)):
                    lines = _read_log(f, 50)
                    if lines:
                        self._json({'ok': True, 'logs': lines, 'path': f})
                        return
            self._json({'ok': True, 'logs': [], 'msg': 'nessun log trovato'})

        else:
            self._not_found()

    # Ã¢ÂÂÃ¢ÂÂ POST Ã¢ÂÂÃ¢ÂÂ
    def do_POST(self):
        p = urlparse(self.path).path.rstrip('/')
        body = self._body()

        if p == '/api/config':
            changed = False
            if 'api_key' in body and body['api_key']:
                _config['api_key']    = str(body['api_key']).strip()
                changed = True
            if 'api_secret' in body and body['api_secret']:
                _config['api_secret'] = str(body['api_secret']).strip()
                changed = True
            if 'testnet' in body:
                _config['testnet'] = bool(body['testnet'])
                changed = True
            if changed:
                save_config(_config)
                log.info(f"Config updated Ã¢ÂÂ key={'SET' if _config['api_key'] else 'EMPTY'} testnet={_config['testnet']}")
            # Scrivi trader_config.json con parametri MM/trading
            try:
                import json as _json
                tc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trader_config.json')
                mm = body.get('mm', {})
                g  = body.get('guardian', {})
                ef = body.get('ef', {})
                tp = body.get('tp', {})
                tc = {
                    'enabled':           True,
                    'size_usdt':         float(mm.get('size', 50)),
                    'leverage':          float(mm.get('leva', 2)),
                    'max_positions':     int(mm.get('maxpos', 2)),
                    'sl_pct':            float(mm.get('sl', 1.2)),
                    'risk_pct':          float(mm.get('risk', 1.0)),
                    'min_funding_abs':   float(ef.get('minrate', 0.003)),
                    'min_persistence':   int(ef.get('persist', 1)),
                    'mins_before_reset': int(ef.get('minreset', 10)),
                    'min_oi_change_5m':  float(ef.get('oi', 0.5)),
                    'guardian': {
                        'max_drawdown':  float(g.get('maxdd', 10)),
                        'max_daily_loss':float(g.get('maxdaily', 100)),
                        'max_cons_loss': int(g.get('maxloss', 3)),
                        'cooldown_min':  int(g.get('cooldown', 30)),
                    },
                    'tp_levels': tp if tp else {
                        'jackpot': [3.0, 1.5, 8.0],
                        'hard':    [2.0, 1.2, 6.0],
                        'extreme': [1.5, 1.0, 4.0],
                        'high':    [0.8, 0.5, 2.5],
                    }
                }
                with open(tc_path, 'w') as tf:
                    _json.dump(tc, tf, indent=2)
                log.info(f"trader_config.json salvato leva={tc['leverage']}x size={tc['size_usdt']}USDT")
            except Exception as _e:
                log.warning(f"trader_config.json error: {_e}")
            self._json({'ok': True, 'msg': 'Config salvata', 'key_set': bool(_config.get('api_key'))})

        elif p == '/api/mode':
            # DISABLED: mode is hardcoded to alert-only
            self._json({'ok': False, 'msg': 'Modalita trading disabilitata - solo alert', 'mode': 'alert'})

        elif p == '/api/risk-params':
            rp = _config.setdefault('risk_params', {})
            for k, v in body.items():
                try: rp[k] = float(v)
                except: pass
            save_config(_config)
            self._json({'ok': True, 'msg': 'Risk params salvati', **rp})

        elif p == '/api/thresholds':
            thr = _config.setdefault('thresholds', {})
            for k, v in body.items():
                try: thr[k] = float(v)
                except: pass
            save_config(_config)
            self._json({'ok': True, 'msg': 'Soglie salvate', **thr})

        elif p == '/api/interval':
            iv = int(body.get('interval', 60))
            iv = max(10, min(3600, iv))
            _config['interval'] = iv
            save_config(_config)
            self._json({'ok': True, 'interval': iv})

        elif p == '/api/watchlist':
            sym = str(body.get('symbol', '')).upper().strip()
            if not sym:
                self._json({'ok': False, 'msg': 'symbol required'}); return
            wl = _config.setdefault('watchlist', [])
            if sym not in wl:
                wl.append(sym)
                save_config(_config)
            self._json({'ok': True, 'watchlist': wl})

        elif p == '/api/alert-config':
            ac = _config.setdefault('alert_config', {})
            if 'enabled' in body:
                ac.setdefault('enabled', {}).update(body['enabled'])
            if 'thresholds' in body:
                ac.setdefault('thresholds', {}).update(body['thresholds'])
            save_config(_config)
            log.info("Alert config updated")
            self._json({'ok': True, 'msg': 'Config salvata', 'config': ac})

        elif p == "/api/auto-trading":
            import re, subprocess
            enabled = bool(body.get("enabled", False))
            env_path = "/root/fundshot/.env"
            try:
                with open(env_path, 'r') as f:
                    env_content = f.read()
                val = 'true' if enabled else 'false'
                env_content = re.sub(r'AUTO_TRADING=\S+', f'AUTO_TRADING={val}', env_content)
                with open(env_path, 'w') as f:
                    f.write(env_content)
                subprocess.Popen(['systemctl', 'restart', 'fundshot'])
                self._json({"ok": True, "auto_trading": val, "msg": f"AUTO_TRADING={val}, bot riavviato"})
            except Exception as e:
                self._json({"ok": False, "msg": str(e)})

        elif p == "/api/close-all":
            # DISABLED: trading disabled, alert-only mode
            self._json({'ok': False, 'msg': 'Trading disabilitato - modalita solo alert'})

        else:
            self._not_found()

    # Ã¢ÂÂÃ¢ÂÂ DELETE Ã¢ÂÂÃ¢ÂÂ
    def do_DELETE(self):
        p = urlparse(self.path).path.rstrip('/')

        if p == '/api/watchlist':
            body = self._body()
            sym  = str(body.get('symbol', '')).upper().strip()
            wl   = _config.get('watchlist', [])
            if sym in wl:
                wl.remove(sym)
                save_config(_config)
            self._json({'ok': True, 'watchlist': wl})

        elif p == '/api/alert-config':
            _config['alert_config'] = DEFAULT_CONFIG['alert_config']
            save_config(_config)
            self._json({'ok': True, 'msg': 'Reset fatto'})

        elif p == '/api/bot-start':
            ok, msg = _bot_start()
            self._json({'ok': ok, 'msg': msg})

        elif p == '/api/bot-stop':
            ok, msg = _bot_stop()
            self._json({'ok': ok, 'msg': msg})

        elif p == '/api/bot-restart':
            _bot_stop()
            time.sleep(1)
            ok, msg = _bot_start()
            self._json({'ok': ok, 'msg': f're-start: {msg}'})

            self._not_found()


# Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
#  MAIN
# Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
_start_time = time.time()

if __name__ == '__main__':
    server = HTTPServer(('0.0.0.0', PORT), ProxyHandler)
    log.info(f"Ã°ÂÂ¥Â FundShot Proxy v5 running on :{PORT}")
    log.info(f"   Config file: {CONFIG_FILE}")
    log.info(f"   API key: {'SET (' + _config['api_key'][:8] + 'Ã¢ÂÂ¦)' if _config.get('api_key') else 'NOT SET'}")
    log.info(f"   Mode: {_config.get('mode','alert')} | Testnet: {_config.get('testnet',False)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Proxy stopped.")
        server.server_close()

# ── PATCH: toggle AUTO_TRADING via dashboard ──
def _set_auto_trading(enabled: bool):
    import re, subprocess, os
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    try:
        with open(env_path, 'r') as f:
            content = f.read()
        val = 'true' if enabled else 'false'
        if 'AUTO_TRADING=' in content:
            content = re.sub(r'AUTO_TRADING=\S+', f'AUTO_TRADING={val}', content)
        else:
            content += f'\nAUTO_TRADING={val}\n'
        with open(env_path, 'w') as f:
            f.write(content)
        subprocess.Popen(['systemctl', 'restart', 'fundshot'])
        return True, val
    except Exception as e:
        return False, str(e)
