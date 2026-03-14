"""
db/supabase_client.py — FundShot SaaS
Gestione utenti multi-tenant su Supabase.

Tabelle gestite:
  - users               → profilo utente (chat_id, plan, exchange attivi)
  - exchange_credentials → API key cifrate per exchange
  - user_settings        → config trading per utente/exchange
  - trades               → storico trade per utente

Le API key vengono cifrate con AES-256-GCM prima di scrivere su DB.
"""

import logging
import os
from typing import Optional
from dataclasses import dataclass, field

from supabase import create_client, Client
from .crypto import encrypt, decrypt

logger = logging.getLogger(__name__)

_client: Optional[Client] = None


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.getenv("SUPABASE_URL", "")
        key = os.getenv("SUPABASE_KEY", "")
        if not url or not key:
            raise EnvironmentError(
                "SUPABASE_URL e SUPABASE_KEY devono essere impostati nel .env"
            )
        _client = create_client(url, key)
    return _client


# ── Dataclass utente ──────────────────────────────────────────────────────────

@dataclass
class User:
    chat_id: int
    telegram_handle: str = ""
    plan: str = "free"          # free | pro | elite
    active_exchanges: list = field(default_factory=list)
    id: str = ""


@dataclass
class ExchangeCredential:
    user_id: str
    exchange: str               # bybit | binance | okx | hyperliquid
    api_key: str                # in chiaro (decrittato)
    api_secret: str             # in chiaro (decrittato)
    passphrase: str = ""        # solo OKX
    wallet: str = ""            # solo Hyperliquid
    environment: str = "demo"   # demo | live
    is_active: bool = True


@dataclass
class UserSettings:
    user_id: str
    exchange: str
    trade_size_usdt: float = 50.0
    leverage: int = 5
    min_level: str = "high"     # base | high | extreme | hard | jackpot
    auto_trading: bool = False
    custom_thresholds: dict = field(default_factory=dict)


# ── USERS ─────────────────────────────────────────────────────────────────────

async def get_or_create_user(chat_id: int, handle: str = "") -> User:
    """Restituisce l'utente esistente o lo crea."""
    db = get_client()
    try:
        res = db.table("users").select("*").eq("chat_id", chat_id).execute()
        if res.data:
            u = res.data[0]
            return User(
                id=u["id"],
                chat_id=u["chat_id"],
                telegram_handle=u.get("telegram_handle", ""),
                plan=u.get("plan", "free"),
                active_exchanges=u.get("active_exchanges", []),
            )
        # Crea nuovo utente
        ins = db.table("users").insert({
            "chat_id": chat_id,
            "telegram_handle": handle,
            "plan": "free",
            "active_exchanges": [],
        }).execute()
        u = ins.data[0]
        logger.info("Nuovo utente creato: chat_id=%s", chat_id)
        return User(
            id=u["id"],
            chat_id=chat_id,
            telegram_handle=handle,
            plan="free",
            active_exchanges=[],
        )
    except Exception as e:
        logger.error("get_or_create_user %s: %s", chat_id, e)
        raise


async def get_user(chat_id: int) -> Optional[User]:
    """Restituisce l'utente o None se non esiste."""
    db = get_client()
    try:
        res = db.table("users").select("*").eq("chat_id", chat_id).execute()
        if not res.data:
            return None
        u = res.data[0]
        return User(
            id=u["id"],
            chat_id=u["chat_id"],
            telegram_handle=u.get("telegram_handle", ""),
            plan=u.get("plan", "free"),
            active_exchanges=u.get("active_exchanges", []),
        )
    except Exception as e:
        logger.error("get_user %s: %s", chat_id, e)
        return None


async def get_all_users() -> list[User]:
    """Tutti gli utenti registrati (per il job multi-tenant)."""
    db = get_client()
    try:
        res = db.table("users").select("*").execute()
        return [
            User(
                id=u["id"],
                chat_id=u["chat_id"],
                telegram_handle=u.get("telegram_handle", ""),
                plan=u.get("plan", "free"),
                active_exchanges=u.get("active_exchanges", []),
            )
            for u in res.data
        ]
    except Exception as e:
        logger.error("get_all_users: %s", e)
        return []


async def update_user_exchanges(user_id: str, exchanges: list[str]) -> bool:
    """Aggiorna la lista di exchange attivi per un utente."""
    db = get_client()
    try:
        db.table("users").update(
            {"active_exchanges": exchanges}
        ).eq("id", user_id).execute()
        return True
    except Exception as e:
        logger.error("update_user_exchanges: %s", e)
        return False


# ── CREDENTIALS ───────────────────────────────────────────────────────────────

async def save_credentials(
    user_id: str,
    exchange: str,
    api_key: str,
    api_secret: str,
    environment: str = "demo",
    passphrase: str = "",
    wallet: str = "",
) -> bool:
    """
    Salva le credenziali cifrate per un exchange.
    Usa upsert: aggiorna se già esistono, inserisce se nuove.
    """
    db = get_client()
    try:
        data = {
            "user_id":         user_id,
            "exchange":        exchange,
            "api_key_enc":     encrypt(api_key),
            "api_secret_enc":  encrypt(api_secret),
            "environment":     environment,
            "is_active":       True,
        }
        if passphrase:
            data["passphrase_enc"] = encrypt(passphrase)
        if wallet:
            data["wallet_enc"] = encrypt(wallet)

        db.table("exchange_credentials").upsert(
            data, on_conflict="user_id,exchange"
        ).execute()
        logger.info("Credenziali %s salvate per user %s", exchange, user_id)
        return True
    except Exception as e:
        logger.error("save_credentials %s/%s: %s", user_id, exchange, e)
        return False


async def get_credentials(
    user_id: str, exchange: str
) -> Optional[ExchangeCredential]:
    """Recupera e decifra le credenziali per un utente/exchange."""
    db = get_client()
    try:
        res = (
            db.table("exchange_credentials")
            .select("*")
            .eq("user_id", user_id)
            .eq("exchange", exchange)
            .eq("is_active", True)
            .execute()
        )
        if not res.data:
            return None
        row = res.data[0]
        return ExchangeCredential(
            user_id=user_id,
            exchange=exchange,
            api_key=decrypt(row.get("api_key_enc", "")),
            api_secret=decrypt(row.get("api_secret_enc", "")),
            passphrase=decrypt(row.get("passphrase_enc", "")),
            wallet=decrypt(row.get("wallet_enc", "")),
            environment=row.get("environment", "demo"),
            is_active=row.get("is_active", True),
        )
    except Exception as e:
        logger.error("get_credentials %s/%s: %s", user_id, exchange, e)
        return None


async def delete_credentials(user_id: str, exchange: str) -> bool:
    """Rimuove (disattiva) le credenziali per un exchange."""
    db = get_client()
    try:
        db.table("exchange_credentials").update(
            {"is_active": False}
        ).eq("user_id", user_id).eq("exchange", exchange).execute()
        return True
    except Exception as e:
        logger.error("delete_credentials: %s", e)
        return False


async def get_all_active_credentials() -> list[ExchangeCredential]:
    """
    Tutte le credenziali attive — usato dal job multi-tenant
    per sapere quali utenti/exchange monitorare.
    """
    db = get_client()
    try:
        res = (
            db.table("exchange_credentials")
            .select("*")
            .eq("is_active", True)
            .execute()
        )
        creds = []
        for row in res.data:
            creds.append(ExchangeCredential(
                user_id=row["user_id"],
                exchange=row["exchange"],
                api_key=decrypt(row.get("api_key_enc", "")),
                api_secret=decrypt(row.get("api_secret_enc", "")),
                passphrase=decrypt(row.get("passphrase_enc", "")),
                wallet=decrypt(row.get("wallet_enc", "")),
                environment=row.get("environment", "demo"),
                is_active=True,
            ))
        return creds
    except Exception as e:
        logger.error("get_all_active_credentials: %s", e)
        return []


# ── USER SETTINGS ─────────────────────────────────────────────────────────────

async def get_user_settings(
    user_id: str, exchange: str
) -> UserSettings:
    """Restituisce le impostazioni trading (con default se non configurate)."""
    db = get_client()
    try:
        res = (
            db.table("user_settings")
            .select("*")
            .eq("user_id", user_id)
            .eq("exchange", exchange)
            .execute()
        )
        if not res.data:
            return UserSettings(user_id=user_id, exchange=exchange)
        row = res.data[0]
        return UserSettings(
            user_id=user_id,
            exchange=exchange,
            trade_size_usdt=row.get("trade_size_usdt", 50.0),
            leverage=row.get("leverage", 5),
            min_level=row.get("min_level", "high"),
            auto_trading=row.get("auto_trading", False),
            custom_thresholds=row.get("custom_thresholds", {}),
        )
    except Exception as e:
        logger.error("get_user_settings: %s", e)
        return UserSettings(user_id=user_id, exchange=exchange)


async def save_user_settings(settings: UserSettings) -> bool:
    """Salva/aggiorna le impostazioni trading di un utente."""
    db = get_client()
    try:
        db.table("user_settings").upsert({
            "user_id":            settings.user_id,
            "exchange":           settings.exchange,
            "trade_size_usdt":    settings.trade_size_usdt,
            "leverage":           settings.leverage,
            "min_level":          settings.min_level,
            "auto_trading":       settings.auto_trading,
            "custom_thresholds":  settings.custom_thresholds,
        }, on_conflict="user_id,exchange").execute()
        return True
    except Exception as e:
        logger.error("save_user_settings: %s", e)
        return False


# ── TRADES ────────────────────────────────────────────────────────────────────

async def record_trade(
    user_id: str,
    exchange: str,
    symbol: str,
    side: str,
    entry_price: float,
    exit_price: float,
    pnl_usdt: float,
    level: str,
    opened_at: str,
    closed_at: str,
    close_reason: str = "",
) -> bool:
    """Registra un trade completato nello storico."""
    db = get_client()
    try:
        db.table("trades").insert({
            "user_id":      user_id,
            "exchange":     exchange,
            "symbol":       symbol,
            "side":         side,
            "entry_price":  entry_price,
            "exit_price":   exit_price,
            "pnl_usdt":     pnl_usdt,
            "level":        level,
            "opened_at":    opened_at,
            "closed_at":    closed_at,
            "close_reason": close_reason,
        }).execute()
        return True
    except Exception as e:
        logger.error("record_trade: %s", e)
        return False


async def get_user_trades(
    user_id: str, exchange: str = "", limit: int = 50
) -> list[dict]:
    """Storico trade per un utente (opzionalmente filtrato per exchange)."""
    db = get_client()
    try:
        q = db.table("trades").select("*").eq("user_id", user_id)
        if exchange:
            q = q.eq("exchange", exchange)
        res = q.order("closed_at", desc=True).limit(limit).execute()
        return res.data
    except Exception as e:
        logger.error("get_user_trades: %s", e)
        return []


# ── PAYMENTS ──────────────────────────────────────────────────────────────────

async def update_user_plan(
    user_id: str,
    plan: str,
    billing_type,          # str | None
    expires_at,            # datetime | None
    subscription_id: str = "",
) -> bool:
    """Aggiorna piano utente dopo pagamento confermato o scadenza."""
    db = get_client()
    try:
        data = {
            "plan":            plan,
            "billing_type":    billing_type,
            "plan_expires_at": expires_at.isoformat() if expires_at and hasattr(expires_at, "isoformat") else None,
            "subscription_id": subscription_id,
        }
        db.table("users").update(data).eq("id", user_id).execute()
        logger.info("Piano aggiornato: user=%s plan=%s expires=%s", user_id, plan, expires_at)
        return True
    except Exception as e:
        logger.error("update_user_plan: %s", e)
        return False


async def save_payment(
    user_id: str,
    chat_id: int,
    nowpay_id: str,
    plan: str,
    billing_type: str,
    amount_usd: float,
    currency: str,
    pay_address: str = "",
    pay_amount: float = 0,
    status: str = "pending",
) -> bool:
    """Registra un pagamento nella tabella payments."""
    db = get_client()
    try:
        db.table("payments").upsert({
            "user_id":      user_id,
            "chat_id":      chat_id,
            "nowpay_id":    nowpay_id,
            "plan":         plan,
            "billing_type": billing_type,
            "amount_usd":   amount_usd,
            "currency":     currency,
            "pay_address":  pay_address,
            "pay_amount":   pay_amount,
            "status":       status,
        }, on_conflict="nowpay_id").execute()
        return True
    except Exception as e:
        logger.error("save_payment: %s", e)
        return False


async def update_payment_status(nowpay_id: str, status: str, actually_paid: float = 0) -> Optional[dict]:
    """Aggiorna lo status di un pagamento e ritorna i dati del pagamento."""
    db = get_client()
    try:
        res = db.table("payments").update({
            "status":        status,
            "actually_paid": actually_paid,
        }).eq("nowpay_id", nowpay_id).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error("update_payment_status: %s", e)
        return None


async def get_user_by_id(user_id: str) -> Optional["User"]:
    """Recupera utente per UUID."""
    db = get_client()
    try:
        res = db.table("users").select("*").eq("id", user_id).single().execute()
        if not res.data:
            return None
        d = res.data
        return User(
            id=d["id"],
            chat_id=d["chat_id"],
            telegram_handle=d.get("telegram_handle", ""),
            plan=d.get("plan", "free"),
            active_exchanges=d.get("active_exchanges") or [],
        )
    except Exception as e:
        logger.error("get_user_by_id: %s", e)
        return None


async def save_user_email(user_id: str, email: str) -> bool:
    """Salva l'email dell'utente su Supabase."""
    db = get_client()
    try:
        db.table("users").update({"email": email}).eq("id", user_id).execute()
        return True
    except Exception as e:
        logger.error("save_user_email: %s", e)
        return False
