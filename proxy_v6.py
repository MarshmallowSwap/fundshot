#!/usr/bin/env python3
"""
proxy_v6.py — FundShot SaaS Proxy
Aggiunge autenticazione JWT via Telegram Login Widget.

Nuovi endpoint rispetto a v5:
  POST /api/auth/telegram   — verifica hash Telegram, ritorna JWT
  GET  /api/me              — info utente autenticato (richiede JWT)
  GET  /api/user/positions  — posizioni dell'utente autenticato
  GET  /api/user/wallet     — saldo dell'utente autenticato
  GET  /api/user/stats      — statistiche trading dell'utente
  GET  /api/user/trades     — storico trade da Supabase

Tutti gli endpoint /api/user/* richiedono Authorization: Bearer <token>
Gli endpoint pubblici (tickers, status) restano accessibili senza auth.

Avvio:
    python3 proxy_v6.py
"""

import json
import logging
import os
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Aggiungi la directory del bot al path
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BOT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(BOT_DIR, ".env"))

from auth import verify_telegram_hash, create_jwt, verify_jwt, extract_token_from_header

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("proxy_v6")

PORT       = int(os.getenv("PROXY_PORT", 8080))
_start_time = time.time()

# ── Cache semplice in memoria ──────────────────────────────────────────────────
_cache: dict = {}
_cache_lock  = threading.Lock()
CACHE_TTL    = 30  # secondi


def cache_get(key: str):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() - entry["ts"] < CACHE_TTL:
            return entry["data"]
    return None


def cache_set(key: str, data):
    with _cache_lock:
        _cache[key] = {"data": data, "ts": time.time()}


# ── Bybit public API ───────────────────────────────────────────────────────────
def bybit_get(path: str, params: dict = None) -> dict:
    from urllib.request import urlopen, Request
    qs  = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    url = f"https://api.bybit.com{path}?{qs}"
    with urlopen(Request(url), timeout=10) as r:
        return json.loads(r.read())


def bybit_get_auth(path: str, params: dict, api_key: str, api_secret: str, demo: bool = True) -> dict:
    """Chiamata Bybit autenticata con le credenziali dell'utente."""
    import hmac
    import hashlib
    from urllib.request import urlopen, Request as UReq
    recv_window = "5000"
    ts          = str(int(time.time() * 1000))
    qs          = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sign_str    = ts + api_key + recv_window + qs
    sig         = hmac.new(api_secret.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    base        = "https://api-demo.bybit.com" if demo else "https://api.bybit.com"
    url         = f"{base}{path}?{qs}"
    req = UReq(url, headers={
        "X-BAPI-API-KEY":     api_key,
        "X-BAPI-TIMESTAMP":   ts,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN":        sig,
    })
    with urlopen(req, timeout=10) as r:
        return json.loads(r.read())


# ── Handler ────────────────────────────────────────────────────────────────────
class ProxyHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.info("%s — %s", self.client_address[0], fmt % args)

    def _headers(self, code=200, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self._headers(code)
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def _auth(self) -> dict | None:
        """
        Verifica JWT dall'header Authorization.
        Restituisce il payload se valido, altrimenti invia 401 e ritorna None.
        """
        token = extract_token_from_header(self.headers.get("Authorization", ""))
        if not token:
            self._json({"ok": False, "error": "Token mancante — fai login con Telegram"}, 401)
            return None
        payload = verify_jwt(token)
        if not payload:
            self._json({"ok": False, "error": "Token non valido o scaduto — rifai login"}, 401)
            return None
        return payload

    # ── OPTIONS (CORS preflight) ──────────────────────────────────────────────

    def do_OPTIONS(self):
        self._headers(204)

    # ── POST ──────────────────────────────────────────────────────────────────

    def do_POST(self):
        p = urlparse(self.path).path.rstrip("/")

        # ── Auth: login via Telegram Login Widget ─────────────────────────────
        if p == "/api/auth/telegram":
            try:
                data = self._body()
                if not verify_telegram_hash(data):
                    self._json({"ok": False, "error": "Hash Telegram non valido"}, 401)
                    return

                chat_id   = int(data.get("id", 0))
                username  = data.get("username", "")
                first_name = data.get("first_name", "")

                # Registra/aggiorna utente su Supabase
                try:
                    import asyncio
                    from db.supabase_client import get_or_create_user
                    user = asyncio.run(get_or_create_user(chat_id, username))
                    user_id = user.id
                    plan    = user.plan
                except Exception as e:
                    log.warning("Supabase get_or_create_user: %s", e)
                    user_id = str(chat_id)
                    plan    = "free"

                token = create_jwt({
                    "chat_id":   chat_id,
                    "user_id":   user_id,
                    "username":  username,
                    "plan":      plan,
                })
                self._json({
                    "ok":        True,
                    "token":     token,
                    "chat_id":   chat_id,
                    "username":  username,
                    "first_name": first_name,
                    "plan":      plan,
                })
                log.info("Login: chat_id=%s username=%s", chat_id, username)
            except Exception as e:
                log.error("auth/telegram: %s", e)
                self._json({"ok": False, "error": str(e)}, 500)
            return

        self._json({"ok": False, "error": "Not Found"}, 404)

    # ── GET ───────────────────────────────────────────────────────────────────

    def do_GET(self):
        p = urlparse(self.path).path.rstrip("/")

        # ── Endpoint pubblici (no auth) ───────────────────────────────────────

        if p == "/api/status":
            self._json({
                "ok":      True,
                "version": "6.0",
                "msg":     "FundShot SaaS proxy running",
                "uptime":  round(time.time() - _start_time, 0),
            })
            return

        if p == "/api/tickers":
            cached = cache_get("tickers")
            if cached:
                self._json(cached)
                return
            try:
                data    = bybit_get("/v5/market/tickers", {"category": "linear"})
                tickers = []
                for t in data.get("result", {}).get("list", []):
                    sym = t.get("symbol", "")
                    if not sym.endswith("USDT"):
                        continue
                    tickers.append({
                        "symbol":          sym,
                        "fundingRate":     float(t.get("fundingRate", 0)),
                        "nextFundingTime": int(t.get("nextFundingTime", 0)),
                        "markPrice":       float(t.get("markPrice", 0)),
                        "price24hPcnt":    float(t.get("price24hPcnt", 0)),
                        "turnover24h":     float(t.get("turnover24h", 0)),
                    })
                result = {"ok": True, "count": len(tickers), "tickers": tickers}
                cache_set("tickers", result)
                self._json(result)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if p == "/api/monitoring":
            try:
                mon_file = "/tmp/fs_monitoring.json"
                data     = json.load(open(mon_file)) if os.path.exists(mon_file) else {}
                self._json({"ok": True, "monitoring": data})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
            return

        if p == "/api/results":
            try:
                data = json.load(open("/tmp/fs_results.json"))
            except Exception:
                data = []
            self._json({"ok": True, "results": data})
            return

        if p == "/api/oi":
            try:
                data = json.load(open("/tmp/fs_oi.json")) if os.path.exists("/tmp/fs_oi.json") else {}
                self._json({"ok": True, "oi": data})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
            return

        # ── Endpoint protetti (richiedono JWT) ────────────────────────────────

        if p == "/api/me":
            user = self._auth()
            if not user:
                return
            try:
                import asyncio
                from db.supabase_client import get_user, get_credentials
                u    = asyncio.run(get_user(user["chat_id"]))
                cred = asyncio.run(get_credentials(u.id, "bybit")) if u else None
                self._json({
                    "ok":               True,
                    "chat_id":          user["chat_id"],
                    "username":         user.get("username", ""),
                    "plan":             user.get("plan", "free"),
                    "active_exchanges": u.active_exchanges if u else [],
                    "bybit_configured": cred is not None,
                    "bybit_env":        cred.environment if cred else None,
                })
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if p == "/api/user/positions":
            user = self._auth()
            if not user:
                return
            try:
                import asyncio
                from db.supabase_client import get_user, get_credentials
                u    = asyncio.run(get_user(user["chat_id"]))
                cred = asyncio.run(get_credentials(u.id, "bybit")) if u else None
                if not cred or not cred.api_key:
                    self._json({"ok": True, "positions": [], "msg": "API key non configurata"})
                    return
                cached = cache_get(f"positions_{user['chat_id']}")
                if cached:
                    self._json(cached)
                    return
                d     = bybit_get_auth(
                    "/v5/position/list",
                    {"category": "linear", "settleCoin": "USDT"},
                    cred.api_key, cred.api_secret,
                    demo=(cred.environment == "demo"),
                )
                items = d.get("result", {}).get("list", [])
                pos   = [
                    {
                        "symbol":       x["symbol"],
                        "side":         x["side"],
                        "size":         x["size"],
                        "avgPrice":     x["avgPrice"],
                        "markPrice":    x["markPrice"],
                        "unrealisedPnl": x["unrealisedPnl"],
                        "leverage":     x["leverage"],
                    }
                    for x in items if float(x.get("size", 0)) != 0
                ]
                result = {"ok": True, "positions": pos, "exchange": "bybit", "env": cred.environment}
                cache_set(f"positions_{user['chat_id']}", result)
                self._json(result)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if p == "/api/user/wallet":
            user = self._auth()
            if not user:
                return
            try:
                import asyncio
                from db.supabase_client import get_user, get_credentials
                u    = asyncio.run(get_user(user["chat_id"]))
                cred = asyncio.run(get_credentials(u.id, "bybit")) if u else None
                if not cred or not cred.api_key:
                    self._json({"ok": False, "error": "API key non configurata"})
                    return
                cached = cache_get(f"wallet_{user['chat_id']}")
                if cached:
                    self._json(cached)
                    return
                d = bybit_get_auth(
                    "/v5/account/wallet-balance",
                    {"accountType": "UNIFIED"},
                    cred.api_key, cred.api_secret,
                    demo=(cred.environment == "demo"),
                )
                a = d.get("result", {}).get("list", [{}])[0]
                result = {
                    "ok":          True,
                    "equity":      a.get("totalEquity", "0"),
                    "available":   a.get("totalAvailableBalance", "0"),
                    "unrealisedPnl": a.get("totalPerpUPL", "0"),
                    "walletBal":   a.get("totalWalletBalance", "0"),
                    "exchange":    "bybit",
                    "env":         cred.environment,
                }
                cache_set(f"wallet_{user['chat_id']}", result)
                self._json(result)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if p == "/api/user/stats":
            user = self._auth()
            if not user:
                return
            try:
                import asyncio
                from db.supabase_client import get_user_trades
                trades = asyncio.run(get_user_trades(user["user_id"], limit=200))
                wins   = [t for t in trades if t.get("pnl_usdt", 0) > 0]
                losses = [t for t in trades if t.get("pnl_usdt", 0) <= 0]
                total  = sum(t.get("pnl_usdt", 0) for t in trades)
                self._json({
                    "ok":       True,
                    "trades":   len(trades),
                    "wins":     len(wins),
                    "losses":   len(losses),
                    "win_rate": round(len(wins)/len(trades)*100, 1) if trades else 0,
                    "total_pnl": round(total, 2),
                    "avg_win":  round(sum(t.get("pnl_usdt",0) for t in wins)/len(wins), 2) if wins else 0,
                    "avg_loss": round(sum(t.get("pnl_usdt",0) for t in losses)/len(losses), 2) if losses else 0,
                })
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if p == "/api/user/trades":
            user = self._auth()
            if not user:
                return
            try:
                import asyncio
                from db.supabase_client import get_user_trades
                qs     = parse_qs(urlparse(self.path).query)
                limit  = int(qs.get("limit", ["50"])[0])
                exchange = qs.get("exchange", [""])[0]
                trades = asyncio.run(get_user_trades(user["user_id"], exchange=exchange, limit=limit))
                self._json({"ok": True, "trades": trades})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return


        if p == "/api/auto-trading":
            user = self._auth()
            if not user:
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                enabled = bool(body.get("enabled", False))
                # Scrivi flag file letto da bot.py ogni ciclo
                flag_file = "/tmp/fs_autotrader.flag"
                with open(flag_file, "w") as f:
                    json.dump({"enabled": enabled, "ts": time.time()}, f)
                log.info("auto-trading toggle: %s", "ON" if enabled else "OFF")
                self._json({"ok": True, "enabled": enabled})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        # ── Backward compat: endpoint v5 senza auth ───────────────────────────
        if p in ("/api/config", "/api/stats", "/api/logs",
                 "/api/bot-status", "/api/alert-config"):
            self._json({"ok": True, "msg": "v6 — usa /api/user/* con JWT", "version": "6.0"})
            return

        self._json({"ok": False, "error": "Not Found"}, 404)


# ── Avvio server ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("🚀 FundShot Proxy v6 — porta %d", PORT)
    server = HTTPServer(("0.0.0.0", PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Proxy fermato.")
        server.server_close()
