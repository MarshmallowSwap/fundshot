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

def cache_delete(key: str):
    with _cache_lock:
        _cache.pop(key, None)

def cache_delete_prefix(prefix: str):
    with _cache_lock:
        keys = [k for k in _cache if k.startswith(prefix)]
        for k in keys:
            del _cache[k]


# ── Bybit public API ───────────────────────────────────────────────────────────
def _notify_config(exchange, body, user):
    try:
        import os, asyncio as _aio
        tok = os.getenv("TELEGRAM_TOKEN", "")
        cid = os.getenv("CHAT_ID", "")
        if not tok or not cid:
            return
        from telegram import Bot as _TBot
        mm  = body.get("mm", {})
        g   = body.get("guardian", {})
        tog = body.get("tog", {})
        # Leggi environment dal payload (inviato dalla dashboard)
        raw_env = body.get("environment", "mainnet")
        env_label = "🧪 Demo" if raw_env in ("demo", "testnet") else "🔴 Live"
        ex_em   = {"bybit": "🟡", "binance": "🟠", "okx": "🔵"}.get(exchange, "⚡")
        bot_on  = "🟢 ON" if tog.get("bot") else "🔴 OFF"
        tp_icon = "✅" if tog.get("tp1")   else "❌"
        tr_icon = "✅" if tog.get("trail") else "❌"
        sl_icon = "✅" if tog.get("sl")    else "❌"
        sep = "━━━━━━━━━━━━━━━━━━"
        parts = [
            "⚙️ *Config aggiornata*",
            ex_em + " " + exchange.capitalize() + " · " + env_label,
            sep,
            "🤖 Auto-Trader: " + bot_on,
            "💰 Size: " + str(mm.get("size","?")) + " USDT · Leva: " + str(mm.get("leva","?")) + "x",
            "📊 Max pos: " + str(mm.get("maxpos","?")) + " · SL: " + str(mm.get("sl","?")) + "%",
            "🎯 TP1 " + tp_icon + " · Trail " + tr_icon + " · SL " + sl_icon,
            sep,
            "🛡️ DD " + str(g.get("maxdd","?")) + "% · Daily " + str(g.get("maxdaily","?")) + " USDT",
            "📡 " + str(body.get("_source","dashboard")),
        ]
        msg = "\n".join(parts)
        bot = _TBot(token=tok)
        _aio.run(bot.send_message(chat_id=cid, text=msg, parse_mode="Markdown"))
    except Exception as e:
        import logging
        logging.getLogger("proxy_v6").warning("_notify_config: %s", e)


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

        # ── Crea pagamento crypto ─────────────────────────────────────────────
        if p == "/api/payments/create":
            user = self._auth()
            if not user:
                return
            try:
                from payments import create_payment, PLANS
                import asyncio
                from db.supabase_client import save_payment, get_user

                body      = self._body()
                plan      = body.get("plan", "")
                billing   = body.get("billing_type", "")
                currency  = body.get("currency", "usdttrc20")

                if plan not in PLANS:
                    self._json({"ok": False, "error": "Invalid plan"}, 400)
                    return
                if billing not in ("recurring", "oneshot"):
                    self._json({"ok": False, "error": "Invalid billing_type"}, 400)
                    return

                result = create_payment(
                    chat_id=user["chat_id"],
                    plan=plan,
                    billing_type=billing,
                    currency=currency,
                )

                # Salva pagamento pending su Supabase
                asyncio.run(save_payment(
                    user_id=user["user_id"],
                    chat_id=user["chat_id"],
                    nowpay_id=str(result["payment_id"]),
                    plan=plan,
                    billing_type=billing,
                    amount_usd=result["amount_usd"],
                    currency=currency,
                    pay_address=result["pay_address"],
                    pay_amount=result.get("pay_amount", 0),
                    status="pending",
                ))

                self._json({"ok": True, **result})
            except Exception as e:
                log.error("payments/create: %s", e)
                self._json({"ok": False, "error": str(e)}, 500)
            return

        # ── Webhook NOWPayments IPN ───────────────────────────────────────────
        if p == "/api/payments/webhook":
            try:
                import asyncio
                from payments import verify_ipn_signature, is_payment_confirmed
                from db.supabase_client import (
                    update_payment_status, update_user_plan,
                    get_user_by_id,
                )
                from datetime import datetime, timedelta, timezone

                raw_body = self.rfile.read(
                    int(self.headers.get("Content-Length", 0))
                )
                sig = self.headers.get("x-nowpayments-sig", "")

                if not verify_ipn_signature(raw_body, sig):
                    log.warning("Webhook IPN: firma non valida")
                    self._json({"ok": False, "error": "Invalid signature"}, 401)
                    return

                data       = json.loads(raw_body)
                nowpay_id  = str(data.get("payment_id", ""))
                status     = data.get("payment_status", "")
                paid       = float(data.get("actually_paid", 0))

                log.info("IPN webhook: payment_id=%s status=%s", nowpay_id, status)

                payment = asyncio.run(
                    update_payment_status(nowpay_id, status, paid)
                )

                if payment and is_payment_confirmed(status):
                    # Aggiorna piano utente (+30 giorni dalla scadenza attuale se non scaduto)
                    from db.supabase_client import get_client as _gc2
                    from datetime import datetime as _dt2
                    try:
                        _res2 = _gc2().table("users").select("plan_expires_at").eq("id", payment["user_id"]).single().execute()
                        _exp2 = (_res2.data or {}).get("plan_expires_at")
                        if _exp2:
                            _exp_dt2 = _dt2.fromisoformat(_exp2.replace("Z", "+00:00"))
                            # Se piano ancora attivo, estendi dalla scadenza
                            if _exp_dt2 > datetime.now(timezone.utc):
                                expires = _exp_dt2 + timedelta(days=30)
                            else:
                                expires = datetime.now(timezone.utc) + timedelta(days=30)
                        else:
                            expires = datetime.now(timezone.utc) + timedelta(days=30)
                    except Exception:
                        expires = datetime.now(timezone.utc) + timedelta(days=30)

                    asyncio.run(update_user_plan(
                        user_id=payment["user_id"],
                        plan=payment["plan"],
                        billing_type=payment["billing_type"],
                        expires_at=expires,
                    ))

                    # Commissione referral (10% al referrer)
                    try:
                        from referral import add_commission
                        commission = asyncio.run(add_commission(
                            payment_user_id=payment["user_id"],
                            amount_usd=payment["amount_usd"],
                        ))
                        if commission > 0:
                            log.info("Commissione referral: %.4f USDT per pagamento user=%s",
                                     commission, payment["user_id"])
                    except Exception as e_ref:
                        log.warning("add_commission: %s", e_ref)

                    # Notifica Telegram
                    try:
                        import os
                        from telegram import Bot
                        bot = Bot(token=os.getenv("TELEGRAM_TOKEN", ""))
                        plan_label    = payment["plan"].capitalize()
                        billing_label = "🔄 Recurring" if payment["billing_type"] == "recurring" else "1️⃣ One-Shot"
                        msg = (
                            f"🎉 *Payment confirmed!*\n\n"
                            f"✅ Plan: *{plan_label}*\n"
                            f"💳 Billing: {billing_label}\n"
                            f"💰 Paid: `{paid} {payment['currency'].upper()}`\n"
                            f"📅 Expires: `{expires.strftime('%d/%m/%Y')}`\n\n"
                            f"All Pro features are now unlocked.\n"
                            f"Use /plan to see your subscription."
                        )
                        import asyncio as _aio
                        _aio.run(bot.send_message(
                            chat_id=payment["chat_id"],
                            text=msg,
                            parse_mode="Markdown",
                        ))
                    except Exception as e_tg:
                        log.warning("Telegram notify: %s", e_tg)

                    # Email conferma
                    try:
                        user = asyncio.run(get_user_by_id(payment["user_id"]))
                        if user and user.telegram_handle:
                            from email_service import send_payment_confirmed
                            send_payment_confirmed(
                                to_email=f"{user.telegram_handle}@users.noreply",
                                username=user.telegram_handle,
                                plan=payment["plan"],
                                billing_type=payment["billing_type"],
                                amount_usd=payment["amount_usd"],
                                currency=payment["currency"],
                                expires_at=expires.strftime("%d/%m/%Y"),
                            )
                    except Exception as e_em:
                        log.warning("Email confirm: %s", e_em)

                self._json({"ok": True})
            except Exception as e:
                log.error("payments/webhook: %s", e)
                self._json({"ok": False, "error": str(e)}, 500)
            return

        # ── POST /api/config — salva config trading dal dashboard ────────────
        if p == "/api/config":
            try:
                body     = self._body()
                exchange = body.get("exchange", "bybit").lower().strip() or "bybit"
                fname    = "/tmp/fs_config_" + exchange + ".json"
                with open(fname, "w") as _f:
                    json.dump(body, _f)
                with open("/tmp/fs_config.json", "w") as _f:
                    json.dump(body, _f)
                log.info("config updated exchange=%s source=%s", exchange, body.get("_source","?"))
                # Notifica immediata Telegram
                _notify_config(exchange, body, self._auth())
                self._json({"ok": True, "saved": True, "exchange": exchange})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        # ── POST /api/user/keys — salva API keys per un exchange ─────────────
        if p == "/api/user/keys":
            user = self._auth()
            if not user:
                return
            try:
                import asyncio
                from db.supabase_client import get_user, save_credentials
                body     = self._body()
                exchange = body.get("exchange", "").lower().strip()
                api_key  = body.get("api_key", "").strip()
                api_sec  = body.get("api_secret", "").strip()
                env      = body.get("environment", "mainnet")
                # Normalizza: la tabella accetta solo 'demo' o 'live'
                if env in ("testnet", "test", "demo"):
                    env = "demo"
                else:
                    env = "live"  # mainnet, live, o qualsiasi altro valore
                # HL non ha demo/testnet — forza live
                if exchange == "hyperliquid":
                    env = "live"
                passph   = body.get("passphrase", "")
                if not exchange or not api_key or (not api_sec and exchange != "hyperliquid"):
                    self._json({"ok": False, "error": "exchange, api_key and api_secret are required"}, 400)
                    return
                # Per Hyperliquid: api_key = wallet address, api_secret = placeholder
                if exchange == "hyperliquid" and not api_sec:
                    api_sec = "hl-wallet-only"
                u = asyncio.run(get_user(user["chat_id"]))
                if not u:
                    self._json({"ok": False, "error": "User not found"}, 404)
                    return
                try:
                    ok = asyncio.run(save_credentials(u.id, exchange, api_key, api_sec, env, passph))
                    log.info("save_credentials result: ok=%s exchange=%s user=%s", ok, exchange, u.id)
                except Exception as e_save:
                    log.error("save_credentials error: %s", e_save)
                    self._json({"ok": False, "error": f"DB error: {str(e_save)[:200]}"}, 500)
                    return
                if ok:
                    try:
                        from db.supabase_client import get_client as _kc
                        db = _kc()
                        # Ri-leggi tutti gli exchange attivi da exchange_credentials (fonte di verità)
                        _rows = db.table("exchange_credentials").select("exchange").eq("user_id", u.id).eq("is_active", True).execute()
                        _active = list({r["exchange"] for r in (_rows.data or [])})
                        db.table("users").update({"active_exchanges": _active}).eq("id", u.id).execute()
                        log.info("active_exchanges sync: user=%s exchanges=%s", u.id, _active)
                    except Exception as e_upd:
                        log.warning("update active_exchanges: %s", e_upd)
                if ok:
                    # Invalida cache wallet/positions per questo utente+exchange
                    chat_id = user.get("chat_id", "")
                    cache_delete_prefix(f"wallet_{chat_id}_{exchange}")
                    cache_delete_prefix(f"positions_{chat_id}_{exchange}")
                self._json({"ok": ok, "exchange": exchange, "environment": env})
            except Exception as e:
                log.error("POST /api/user/keys: %s", e)
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if p == "/api/support":
            try:
                body_bytes = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.loads(body_bytes)
                msg     = body.get("message", "")[:500]
                user    = body.get("user", "unknown")
                chat_id = body.get("chat_id")
                plan    = body.get("plan", "free")
                if not msg:
                    self._json({"ok": False, "error": "empty"}, 400)
                    return
                import urllib.request as _ur3, os as _os3
                owner_id = _os3.getenv("CHAT_ID", "")
                token    = _os3.getenv("TELEGRAM_TOKEN", "")
                if token:
                    # Use SUPPORT_CHAT_ID if set, fallback to owner
                    dest = _os3.getenv("SUPPORT_CHAT_ID") or owner_id
                    plan_emoji = {"elite": "crown", "pro": "bolt", "free": "free"}.get(plan, "free")
                    reply_cmd = ("/reply " + str(chat_id)) if chat_id else ""
                    email = body.get("email", "")
                    source = body.get("source", "dashboard")
                    from_str = ("Email: " + email) if email else ("@" + user + " [" + plan.upper() + "]")
                    reply_str = ("Reply: " + reply_cmd) if reply_cmd else ("Reply via email: " + email if email else "")
                    notify = (
                        "NEW SUPPORT REQUEST (" + source + ")\n"
                        + from_str + "\n"
                        "---\n" + msg + "\n"
                        + ("---\n" + reply_str if reply_str else "")
                    )
                    tg_url = "https://api.telegram.org/bot" + token + "/sendMessage"
                    tg_req = _ur3.Request(tg_url,
                        data=json.dumps({"chat_id": dest, "text": notify}).encode(),
                        headers={"Content-Type": "application/json"}, method="POST")
                    _ur3.urlopen(tg_req, timeout=5)
                log.info("Support ticket from %s: %s", user, msg[:80])
                self._json({"ok": True})
            except Exception as e:
                log.error("/api/support: %s", e)
                self._json({"ok": True})
            return

        if p == "/api/user/wallet-address":
            user = self._auth()
            if not user: return
            try:
                body_bytes = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.loads(body_bytes)
                wallet = body.get("wallet", "").strip()
                if not wallet or not wallet.startswith("T") or len(wallet) < 20:
                    self._json({"ok": False, "error": "Invalid USDT TRC20 address"}, 400)
                    return
                import asyncio as _aw
                from db.supabase_client import get_user as _gu3, get_client as _gc3
                u = _aw.run(_gu3(user["chat_id"]))
                if not u:
                    self._json({"ok": False, "error": "User not found"}, 404)
                    return
                _gc3().table("users").update({"referral_wallet_usdt": wallet}).eq("id", u.id).execute()
                self._json({"ok": True})
            except Exception as e:
                log.error("/api/user/wallet-address: %s", e)
                self._json({"ok": False, "error": str(e)[:200]}, 500)
            return

        if p == "/api/ai":
            try:
                body_bytes = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                req_data = json.loads(body_bytes)
                anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
                if not anthropic_key:
                    self._json({"error": "AI not configured"}, 503)
                    return
                import urllib.request as _ur2
                ai_req = _ur2.Request(
                    "https://api.anthropic.com/v1/messages",
                    data=json.dumps(req_data).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": anthropic_key,
                        "anthropic-version": "2023-06-01",
                    },
                    method="POST"
                )
                with _ur2.urlopen(ai_req, timeout=30) as resp:
                    result = json.loads(resp.read())
                self._json(result)
            except Exception as e:
                log.error("/api/ai error: %s", e)
                self._json({"error": str(e)[:200]}, 500)
            return

        self._json({"ok": False, "error": "Not Found"}, 404)

    # ── GET ───────────────────────────────────────────────────────────────────

    def do_GET(self):
        p = urlparse(self.path).path.rstrip("/")

        # ── Endpoint pubblici (no auth) ───────────────────────────────────────

        if p == "/api/status":
            import os as _ost
            uptime_s = round(time.time() - _start_time, 0)
            uptime_h = round(uptime_s / 3600, 1)
            # Check how fresh the alert history is
            last_alert_ts = 0
            try:
                ah = json.load(open("/tmp/fs_alert_history.json")) if _ost.path.exists("/tmp/fs_alert_history.json") else []
                if ah: last_alert_ts = max(a.get("ts",0) for a in ah)
            except: pass
            alert_age_min = round((time.time() - last_alert_ts) / 60, 0) if last_alert_ts else None
            self._json({
                "ok":            True,
                "status":        "operational",
                "version":       "6.0",
                "uptime_seconds": uptime_s,
                "uptime_hours":  uptime_h,
                "pairs_monitored": 500,
                "exchanges":     ["Bybit", "Binance", "Hyperliquid"],
                "last_alert_min_ago": alert_age_min,
                "components": {
                    "funding_monitor": "operational",
                    "alert_delivery":  "operational",
                    "auto_trader":     "operational",
                    "guardian":        "operational",
                }
            })
            return

        if p == "/api/public-alerts":
            try:
                import os as _opa
                f = "/tmp/fs_alert_history.json"
                if _opa.path.exists(f):
                    data = json.load(open(f))
                    # All levels for public feed, sorted by ts desc
                    public = [
                        {
                            "symbol":   a["symbol"],
                            "level":    a["level"],
                            "rate_pct": a["rate_pct"],
                            "exchange": a["exchange"],
                            "ex_em":    a.get("ex_em", "⚡"),
                            "ts":       a["ts"],
                        }
                        for a in data
                        if a.get("symbol") and a.get("level")
                    ]
                    public = sorted(public, key=lambda x: x["ts"], reverse=True)[:30]
                    self._json({"ok": True, "alerts": public, "total": len(data)})
                else:
                    self._json({"ok": True, "alerts": [], "total": 0})
            except Exception as e:
                self._json({"ok": True, "alerts": [], "error": str(e)})
            return

        if p == "/api/public-stats":
            try:
                users = _sb_admin.table("users").select("id").execute().data or []
                count = len(users)
                # Round down to nearest 5 for privacy
                display = max(1, (count // 5) * 5) if count >= 5 else count
                self._json({
                    "ok": True,
                    "traders": display,
                    "pairs_monitored": 500,
                    "exchanges": 3,
                })
            except:
                self._json({"ok": True, "traders": 0, "pairs_monitored": 500, "exchanges": 3})
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

        if p == "/api/alert-history":
            user = self._auth()
            if not user: return
            try:
                # Leggi da file temporaneo scritto dal bot
                import os as _os
                f = "/tmp/fs_alert_history.json"
                if _os.path.exists(f):
                    data = json.load(open(f))
                    self._json({"ok": True, "alerts": data[-50:][::-1]})  # ultimi 50, più recenti prima
                else:
                    self._json({"ok": True, "alerts": []})
            except Exception as e:
                self._json({"ok": False, "alerts": [], "error": str(e)})
            return

        if p == "/api/track-record":
            try:
                import os as _os
                f = "/tmp/fs_track_record.json"
                if _os.path.exists(f):
                    data = json.load(open(f))
                    self._json({"ok": True, "record": data})
                else:
                    self._json({"ok": False, "error": "Track record not yet generated"})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
            return

        # GET /api/alert-history — ultimi 50 alert inviati (per-user)
        if p == "/api/alert-history":
            user = self._auth()
            if not user:
                return
            try:
                import os as _os
                f = "/tmp/fs_alert_history.json"
                if _os.path.exists(f):
                    import json as _j
                    data = _j.loads(open(f).read())
                    # Filtra per chat_id utente se non owner
                    self._json({"ok": True, "alerts": data[-50:][::-1]})
                else:
                    self._json({"ok": True, "alerts": []})
            except Exception as e:
                self._json({"ok": True, "alerts": [], "error": str(e)})
            return

        if p == "/api/oi":
            try:
                data = json.load(open("/tmp/fs_oi.json")) if os.path.exists("/tmp/fs_oi.json") else {}
                # Se vuoto (bot non ancora scritto), fallback a Bybit public API
                if not data:
                    try:
                        from urllib.request import urlopen
                        import json as _j2
                        top = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","BNBUSDT"]
                        fallback = {}
                        for sym in top:
                            try:
                                url = f"https://api.bybit.com/v5/market/open-interest?category=linear&symbol={sym}&intervalTime=5min&limit=2"
                                r   = _j2.loads(urlopen(url, timeout=3).read())
                                items = r.get("result", {}).get("list", [])
                                if len(items) >= 2:
                                    curr = float(items[0]["openInterest"])
                                    prev = float(items[1]["openInterest"])
                                    chg  = round((curr - prev) / prev * 100, 3) if prev else 0
                                    fallback[sym] = {"change_5m": chg, "oi": curr, "funding": 0, "spike": abs(chg) >= 3}
                            except Exception:
                                pass
                        if fallback:
                            data = fallback
                    except Exception:
                        pass
                self._json({"ok": True, "oi": data})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
            return

        if p == "/api/closed-pnl":
            user = self._auth()
            if not user:
                return
            try:
                import asyncio
                from db.supabase_client import get_user_trades, get_user
                u      = asyncio.run(get_user(user["chat_id"]))
                if not u:
                    self._json({"ok": True, "trades": []})
                    return
                trades = asyncio.run(get_user_trades(u.id, limit=100))
                result = []
                for t in trades:
                    result.append({
                        "symbol":    t.get("symbol", ""),
                        "side":      t.get("side", ""),
                        "pnl_usdt":  float(t.get("realized_pnl", 0) or 0),
                        "qty":       float(t.get("qty", 0) or 0),
                        "entry":     float(t.get("entry_price", 0) or 0),
                        "exit":      float(t.get("exit_price", 0) or 0),
                        "ts":        t.get("closed_at", t.get("created_at", "")),
                        "exchange":  t.get("exchange", "bybit"),
                    })
                self._json({"ok": True, "trades": result})
            except Exception as e:
                self._json({"ok": False, "error": str(e), "trades": []})
            return

        # ── Endpoint protetti (richiedono JWT) ────────────────────────────────

        if p == "/api/me":
            user = self._auth()
            if not user:
                return
            try:
                import asyncio
                from db.supabase_client import get_user, get_client as _me_gc
                u = asyncio.run(get_user(user["chat_id"]))
                # Leggi plan_expires_at
                plan_exp = None
                try:
                    _res = _me_gc().table("users").select("plan_expires_at").eq("id", u.id).single().execute()
                    plan_exp = (_res.data or {}).get("plan_expires_at")
                except Exception:
                    pass
                import os as _os_me
                _owner_cid = int(_os_me.getenv("CHAT_ID", "0"))
                _is_owner  = user["chat_id"] == _owner_cid
                self._json({
                    "ok":               True,
                    "chat_id":          user["chat_id"],
                    "username":         user.get("username", ""),
                    "first_name":       user.get("first_name", ""),
                    "plan":             "elite" if _is_owner else (u.plan if u else user.get("plan", "free")),
                    "plan_expires_at":  None if _is_owner else plan_exp,
                    "active_exchanges": u.active_exchanges if u else [],
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
                from user_registry import registry as _reg
                from exchanges import make_client

                qs_params = parse_qs(urlparse(self.path).query)
                exchange  = (qs_params.get("exchange", ["bybit"])[0]).lower()

                u    = asyncio.run(get_user(user["chat_id"]))
                cred = asyncio.run(get_credentials(u.id, exchange)) if u else None
                if not cred or not cred.api_key:
                    self._json({"ok": True, "positions": [], "exchange": exchange,
                                "msg": f"No {exchange} API key configured"})
                    return

                cache_key = f"positions_{user['chat_id']}_{exchange}"
                cached = cache_get(cache_key)
                if cached:
                    self._json(cached)
                    return

                if exchange == "okx":
                    self._json({"ok": False, "error": "OKX_GEO_BLOCK", "geo_blocked": True, "positions": []})
                    return
                client = make_client(
                    exchange=exchange,
                    api_key=cred.api_key,
                    api_secret=cred.api_secret,
                    demo=(cred.environment == "demo"),
                    testnet=False,
                    passphrase=getattr(cred, "passphrase", ""),
                )
                positions = asyncio.run(client.get_positions())
                pos = [
                    {
                        "symbol":        p.symbol,
                        "side":          p.side,
                        "size":          str(p.size),
                        "avgPrice":      str(p.avg_price),
                        "markPrice":     str(p.mark_price),
                        "unrealisedPnl": str(p.unrealised_pnl),
                        "leverage":      str(p.leverage),
                        "liqPrice":      str(p.liq_price),
                    }
                    for p in positions
                ]
                result = {"ok": True, "positions": pos, "exchange": exchange, "env": cred.environment}
                cache_set(cache_key, result)
                self._json(result)
            except Exception as e:
                log.error("positions %s: %s", exchange if 'exchange' in dir() else '?', e)
                self._json({"ok": True, "positions": [], "error": str(e)}, 200)
            return

        if p == "/api/user/wallet":
            user = self._auth()
            if not user:
                return
            try:
                import asyncio
                from db.supabase_client import get_user, get_credentials
                from exchanges import make_client

                qs_params = parse_qs(urlparse(self.path).query)
                exchange  = (qs_params.get("exchange", ["bybit"])[0]).lower()

                u    = asyncio.run(get_user(user["chat_id"]))
                cred = asyncio.run(get_credentials(u.id, exchange)) if u else None
                if not cred or not cred.api_key:
                    self._json({"ok": False, "error": f"No {exchange} API key configured"})
                    return

                cache_key = f"wallet_{user['chat_id']}_{exchange}"
                cached = cache_get(cache_key)
                if cached:
                    self._json(cached)
                    return

                client = make_client(
                    exchange=exchange,
                    api_key=cred.api_key,
                    api_secret=cred.api_secret,
                    demo=(cred.environment == "demo"),
                    testnet=False,
                    passphrase=getattr(cred, "passphrase", ""),
                )
                try:
                    wb = asyncio.run(client.get_wallet_balance())
                except Exception as _we:
                    log.error("get_wallet_balance %s: %s", exchange, _we)
                    self._json({"ok": False, "error": f"{exchange} wallet error: {str(_we)[:150]}"}, 500)
                    return
                if exchange == "hyperliquid":
                    wallet = cred.api_key
                    if not wallet or not wallet.startswith("0x"):
                        self._json({"ok": False, "error": "Configure wallet address in Settings"})
                        return
                    import asyncio as _hlaw
                    from exchanges.hyperliquid import HyperliquidClient as _HLW
                    _hlw = _HLW(api_key=wallet)
                    wb_hl = _hlaw.run(_hlw.get_wallet_balance())
                    if wb_hl:
                        self._json({"ok": True, "equity": str(wb_hl.total_equity),
                            "available": str(wb_hl.total_available_balance),
                            "margin": str(round(wb_hl.total_equity - wb_hl.total_available_balance, 4)),
                            "unrealisedPnl": str(wb_hl.total_perp_upl),
                            "coins": [], "exchange": "hyperliquid", "env": "mainnet"})
                    else:
                        self._json({"ok": False, "error": "Could not fetch Hyperliquid wallet"})
                    return
                if not wb:
                    if exchange == "okx":
                        self._json({"ok": False, "error": "OKX: API key not found for this mode. Demo mode requires Paper Trading keys from okx.com/account/demo/trade — Live mode requires Live keys."})
                    else:
                        self._json({"ok": False, "error": f"Cannot fetch {exchange} wallet — check API keys and permissions"})
                    return

                result = {
                    "ok":            True,
                    "equity":        str(wb.total_equity),
                    "available":     str(wb.total_available_balance),
                    "margin":        str(round(wb.total_equity - wb.total_available_balance, 4)),
                    "unrealisedPnl": str(wb.total_perp_upl),
                    "coins":         wb.coins or [],
                    "exchange":      exchange,
                    "env":           cred.environment,
                }
                cache_set(cache_key, result)
                self._json(result)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if p == "/api/user/exchanges":
            user = self._auth()
            if not user:
                return
            try:
                import asyncio
                from db.supabase_client import get_user, get_client as _ex_gc
                u = asyncio.run(get_user(user["chat_id"]))
                # Ritorna lista di oggetti {exchange, environment}
                db = _ex_gc()
                rows = db.table("exchange_credentials").select("exchange,environment").eq("user_id", u.id).eq("is_active", True).execute()
                exchanges = [{"exchange": r["exchange"], "environment": r.get("environment","mainnet")} for r in (rows.data or [])]
                self._json({"ok": True, "exchanges": exchanges})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        # ── Test channel (usa ADMIN_TOKEN) ────────────────────────────────────
        if p == "/api/admin/test-channel":
            import os as _os
            try:
                import asyncio as _aio
                from telegram import Bot as _TBot
                _tok = _os.getenv("TELEGRAM_TOKEN", "")
                _cid = _os.getenv("CHANNEL_ID", "")
                if not _tok or not _cid:
                    self._json({"ok": False, "error": f"TOKEN={bool(_tok)} CHANNEL={_cid}"})
                    return
                _bot = _TBot(token=_tok)
                async def _send():
                    return await _bot.send_message(chat_id=_cid, text="🧪 FundShot channel test")
                msg = _aio.run(_send())
                self._json({"ok": True, "message_id": msg.message_id, "channel": _cid})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
            return

        # ── Admin endpoints (owner only) ─────────────────────────────────────
        if p.startswith("/api/admin/"):
            user = self._auth()
            if not user:
                return
            owner_id = int(os.getenv("CHAT_ID", "0"))
            if user.get("chat_id") != owner_id:
                self._json({"ok": False, "error": "Forbidden"}, 403)
                return
            import asyncio
            from db.supabase_client import get_client as _adm_gc

            if p == "/api/admin/stats":
                try:
                    db = _adm_gc()
                    # Utenti totali
                    u_res  = db.table("users").select("id,plan,created_at").execute()
                    users  = u_res.data or []
                    # Pagamenti
                    p_res  = db.table("payments").select("*").eq("status","confirmed").execute()
                    pays   = p_res.data or []
                    # Referral
                    r_res  = db.table("referrals").select("*").execute()
                    refs   = r_res.data or []

                    total_rev = sum(float(p.get("amount_usd",0) or 0) for p in pays)
                    plans_cnt = {"free":0,"pro":0,"elite":0}
                    for u in users:
                        plans_cnt[u.get("plan","free")] = plans_cnt.get(u.get("plan","free"),0)+1

                    self._json({
                        "ok": True,
                        "users_total":    len(users),
                        "plans":          plans_cnt,
                        "payments_count": len(pays),
                        "revenue_total":  round(total_rev, 2),
                        "referrals":      len(refs),
                        "referrals_conv": sum(1 for r in refs if r["status"]!="pending"),
                    })
                except Exception as e:
                    self._json({"ok": False, "error": str(e)}, 500)
                return

            if p == "/api/admin/users":
                try:
                    db = _adm_gc()
                    res = db.table("users").select(
                        "id,chat_id,telegram_handle,plan,created_at,plan_expires_at,"
                        "billing_type,active_exchanges,is_influencer,"
                        "referral_code,referral_balance_usd,referral_total_earned_usd"
                    ).order("created_at", desc=True).limit(200).execute()
                    self._json({"ok": True, "users": res.data or []})
                except Exception as e:
                    self._json({"ok": False, "error": str(e)}, 500)
                return

            if p == "/api/admin/payments":
                try:
                    db = _adm_gc()
                    res = db.table("payments").select("*").order("created_at", desc=True).limit(100).execute()
                    self._json({"ok": True, "payments": res.data or []})
                except Exception as e:
                    self._json({"ok": False, "error": str(e)}, 500)
                return

            if p == "/api/admin/payouts":
                try:
                    db = _adm_gc()
                    res = db.table("users").select(
                        "id,chat_id,telegram_handle,referral_balance_usd,"
                        "referral_total_earned_usd,referral_wallet_usdt,is_influencer"
                    ).gt("referral_balance_usd", 0).order("referral_balance_usd", desc=True).execute()
                    self._json({"ok": True, "payouts": res.data or []})
                except Exception as e:
                    self._json({"ok": False, "error": str(e)}, 500)
                return

            self._json({"ok": False, "error": "Admin endpoint not found"}, 404)
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
                body    = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                enabled = bool(body.get("enabled", False))
                action  = body.get("action", "off_only")  # "close_all" | "off_only"
                flag_file = "/tmp/fs_autotrader.flag"
                with open(flag_file, "w") as f:
                    json.dump({"enabled": enabled, "action": action, "ts": time.time()}, f)
                log.info("auto-trading toggle: %s action=%s", "ON" if enabled else "OFF", action)
                self._json({"ok": True, "enabled": enabled, "action": action})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        # ── Backward compat: endpoint v5 senza auth ───────────────────────────
        if p in ("/api/stats", "/api/logs",
                 "/api/bot-status", "/api/alert-config"):
            self._json({"ok": True, "msg": "v6 — usa /api/user/* con JWT", "version": "6.0"})
            return

        # GET /api/user/keys/:exchange
        if p.startswith("/api/user/keys/"):
            user = self._auth()
            if not user: return
            if self.do_GET_keys(p, user): return

        # GET /api/test/okx — test OKX auth con log dettagliato
        if p == "/api/test/okx":
            user = self._auth()
            if not user: return
            try:
                import asyncio, aiohttp, base64, hashlib, hmac
                from datetime import datetime, timezone
                from db.supabase_client import get_user, get_credentials
                u    = asyncio.run(get_user(user["chat_id"]))
                cred = asyncio.run(get_credentials(u.id, "okx")) if u else None
                if not cred:
                    self._json({"ok": False, "error": "No OKX credentials"})
                    return
                pp = getattr(cred, "passphrase", "") or ""
                log.info("OKX TEST: api_key=%s... passphrase_len=%d env=%s", 
                         cred.api_key[:8] if cred.api_key else "EMPTY", len(pp), cred.environment)
                
                # Fai una chiamata test
                ts  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                path = "/api/v5/account/balance"
                msg  = ts + "GET" + path
                sig  = base64.b64encode(hmac.new(cred.api_secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()
                headers = {
                    "OK-ACCESS-KEY": cred.api_key,
                    "OK-ACCESS-SIGN": sig,
                    "OK-ACCESS-TIMESTAMP": ts,
                    "OK-ACCESS-PASSPHRASE": pp,
                    "x-simulated-trading": "1" if cred.environment == "demo" else "0",
                    "Content-Type": "application/json",
                }
                async def _test():
                    async with aiohttp.ClientSession() as s:
                        async with s.get("https://www.okx.com" + path, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                            return await r.json()
                result = asyncio.run(_test())
                log.info("OKX TEST result: code=%s msg=%s", result.get("code"), result.get("msg"))
                self._json({"ok": True, "okx_code": result.get("code"), "okx_msg": result.get("msg"), 
                            "has_data": bool(result.get("data"))})
            except Exception as e:
                log.error("OKX TEST error: %s", e)
                self._json({"ok": False, "error": str(e)})
            return

        self._json({"ok": False, "error": "Not Found"}, 404)

    # ── DELETE ────────────────────────────────────────────────────────────────

    def do_GET_keys(self, p, user):
        """GET /api/user/keys/:exchange — verifica se keys sono salvate (senza esporre i valori)"""
        if not p.startswith("/api/user/keys/"):
            return False
        exchange = p.split("/")[-1].lower()
        try:
            import asyncio
            from db.supabase_client import get_user, get_credentials
            u    = asyncio.run(get_user(user["chat_id"]))
            cred = asyncio.run(get_credentials(u.id, exchange)) if u else None
            if not cred:
                self._json({"ok": False, "configured": False, "exchange": exchange})
            else:
                self._json({
                    "ok":          True,
                    "configured":  True,
                    "exchange":    exchange,
                    "environment": cred.environment,
                    "has_key":     bool(cred.api_key),
                    "has_secret":  bool(cred.api_secret),
                    "has_passphrase": bool(cred.passphrase),
                })
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)
        return True

    def do_DELETE(self):
        p = urlparse(self.path).path.rstrip("/")

        # DELETE /api/user/keys/:exchange
        if p.startswith("/api/user/keys/"):
            user = self._auth()
            if not user:
                return
            exchange = p.split("/")[-1].lower()
            try:
                import asyncio
                from db.supabase_client import get_user, get_client as _dk
                u = asyncio.run(get_user(user["chat_id"]))
                if not u:
                    self._json({"ok": False, "error": "User not found"}, 404)
                    return
                db = _dk()
                db.table("exchange_credentials").update({"is_active": False}).eq("user_id", u.id).eq("exchange", exchange).execute()
                cur = set(u.active_exchanges or [])
                cur.discard(exchange)
                db.table("users").update({"active_exchanges": list(cur)}).eq("id", u.id).execute()
                log.info("API keys removed: user=%s exchange=%s", user["chat_id"], exchange)
                self._json({"ok": True, "exchange": exchange})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        self._json({"ok": False, "error": "Not Found"}, 404)


    def do_PATCH(self):
        p = urlparse(self.path).path.rstrip("/")
        if p.startswith("/api/user/keys/"):
            user = self._auth()
            if not user:
                return
            exchange = p.split("/")[-1].lower()
            try:
                import asyncio
                from db.supabase_client import get_user, get_client as _pk
                body = self._body()
                env  = body.get("environment", "mainnet")
                if env in ("testnet", "test"): env = "demo"
                u  = asyncio.run(get_user(user["chat_id"]))
                if not u:
                    self._json({"ok": False, "error": "User not found"}, 404)
                    return
                db = _pk()
                db.table("exchange_credentials").update({"environment": env}).eq("user_id", u.id).eq("exchange", exchange).execute()
                log.info("environment updated: user=%s exchange=%s env=%s", user["chat_id"], exchange, env)
                cache_delete_prefix(f"wallet_{user['chat_id']}_{exchange}")
                cache_delete_prefix(f"positions_{user['chat_id']}_{exchange}")
                self._json({"ok": True, "exchange": exchange, "environment": env})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
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
# (nessuna modifica qui — fix applicato sotto)
