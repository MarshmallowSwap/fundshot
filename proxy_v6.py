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
                from urllib.parse import parse_qs, urlparse
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

                client = make_client(
                    exchange=exchange,
                    api_key=cred.api_key,
                    api_secret=cred.api_secret,
                    demo=(cred.environment == "demo"),
                    testnet=False,
                )
                positions = asyncio.run(client.get_positions())
                pos = [
                    {
                        "symbol":        p.symbol,
                        "side":          p.side,
                        "size":          str(p.size),
                        "avgPrice":      str(p.entry_price),
                        "markPrice":     str(p.mark_price),
                        "unrealisedPnl": str(p.unrealized_pnl),
                        "leverage":      str(p.leverage),
                        "liqPrice":      str(p.liq_price),
                    }
                    for p in positions
                ]
                result = {"ok": True, "positions": pos, "exchange": exchange, "env": cred.environment}
                cache_set(cache_key, result)
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
                from urllib.parse import parse_qs, urlparse
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
                )
                wb = asyncio.run(client.get_wallet_balance())
                if not wb:
                    self._json({"ok": False, "error": "Cannot fetch wallet"})
                    return

                result = {
                    "ok":           True,
                    "equity":       str(wb.total_equity),
                    "available":    str(wb.available_balance),
                    "unrealisedPnl": str(wb.unrealized_pnl),
                    "exchange":     exchange,
                    "env":          cred.environment,
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
                from db.supabase_client import get_user
                u = asyncio.run(get_user(user["chat_id"]))
                exchanges = u.active_exchanges if u else []
                self._json({"ok": True, "exchanges": exchanges})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
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
