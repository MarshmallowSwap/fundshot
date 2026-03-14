"""
payments.py — FundShot SaaS
Integrazione NOWPayments per pagamenti crypto.

Piani:
  Pro   — $15 USDT/mese (recurring) | $20 USDT (oneshot)
  Elite — $40 USDT/mese (recurring) | $50 USDT (oneshot)

Crypto accettate: USDT, BTC, ETH, SOL, BNB, TON
"""

import hashlib
import hmac
import json
import logging
import os
from typing import Optional
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

NOWPAY_API_KEY = os.getenv("NOWPAY_API_KEY", "")
NOWPAY_IPN_SECRET = os.getenv("NOWPAY_IPN_SECRET", "")
NOWPAY_BASE = "https://api.nowpayments.io/v1"

# ── Prezzi piani ──────────────────────────────────────────────────────────────
PLANS = {
    "pro": {
        "name": "Pro",
        "recurring": 20.0,
        "oneshot":   25.0,
        "duration_days": 30,
    },
    "elite": {
        "name": "Elite",
        "recurring": 45.0,
        "oneshot":   55.0,
        "duration_days": 30,
    },
}

# Crypto supportate con label display
CURRENCIES = {
    "usdttrc20": "USDT (TRC20 — Tron)",
    "usdterc20": "USDT (ERC20 — Ethereum)",
    "usdtsol":   "USDT (SOL — Solana)",
    "btc":       "Bitcoin (BTC)",
    "eth":       "Ethereum (ETH)",
    "sol":       "Solana (SOL)",
    "bnbbsc":    "BNB (BEP20 — BSC)",
    "ton":       "TON",
}


def _request(method: str, path: str, body: dict = None) -> dict:
    """HTTP request verso NOWPayments API."""
    url = NOWPAY_BASE + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "x-api-key":    NOWPAY_API_KEY,
            "Content-Type": "application/json",
            "Accept":       "application/json",
            "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_err = e.read().decode()
        logger.error("NOWPayments %s %s → %s: %s", method, path, e.code, body_err)
        raise RuntimeError(f"NOWPayments error {e.code}: {body_err}")
    except Exception as e:
        logger.error("NOWPayments request error: %s", e)
        raise


def create_payment(
    chat_id: int,
    plan: str,
    billing_type: str,
    currency: str,
) -> dict:
    """
    Crea un pagamento su NOWPayments.
    Ritorna: { payment_id, pay_address, pay_amount, pay_currency, expiry_time }
    """
    plan_cfg  = PLANS.get(plan)
    if not plan_cfg:
        raise ValueError(f"Piano sconosciuto: {plan}")
    if billing_type not in ("recurring", "oneshot"):
        raise ValueError(f"Tipo billing sconosciuto: {billing_type}")

    amount_usd = plan_cfg[billing_type]
    order_id   = f"fs_{chat_id}_{plan}_{billing_type}"

    payload = {
        "price_amount":   amount_usd,
        "price_currency": "usd",
        "pay_currency":   currency,
        "order_id":       order_id,
        "order_description": f"FundShot {plan_cfg['name']} — {billing_type} 30 days",
        "ipn_callback_url": "https://api.fundshot.app/api/payments/webhook",
        "success_url":    "https://fundshot.app?payment=success",
        "cancel_url":     "https://fundshot.app?payment=cancelled",
        "is_fixed_rate":  False,
        "is_fee_paid_by_user": False,
    }

    result = _request("POST", "/payment", payload)
    return {
        "payment_id":   result.get("payment_id"),
        "pay_address":  result.get("pay_address"),
        "pay_amount":   result.get("pay_amount"),
        "pay_currency": result.get("pay_currency", currency).upper(),
        "amount_usd":   amount_usd,
        "expiry":       result.get("expiration_estimate_date", ""),
        "status":       result.get("payment_status", "pending"),
    }


def get_payment_status(payment_id: str) -> dict:
    """Verifica lo stato di un pagamento."""
    return _request("GET", f"/payment/{payment_id}")


def verify_ipn_signature(payload_bytes: bytes, received_sig: str) -> bool:
    """
    Verifica la firma HMAC-SHA512 del webhook IPN di NOWPayments.
    """
    if not NOWPAY_IPN_SECRET:
        logger.warning("NOWPAY_IPN_SECRET non configurato — skip verifica")
        return True
    expected = hmac.new(
        NOWPAY_IPN_SECRET.encode(),
        payload_bytes,
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(expected, received_sig.lower())


def is_payment_confirmed(status: str) -> bool:
    """Ritorna True se il pagamento è confermato."""
    return status in ("confirmed", "finished")


def currency_display(currency: str) -> str:
    return CURRENCIES.get(currency.lower(), currency.upper())


# ── Subscription (Recurring) ──────────────────────────────────────────────────

# Plan IDs NOWPayments — creati una volta sola (vedi setup_subscription_plans)
_SUBSCRIPTION_PLAN_IDS: dict[str, str] = {}


def setup_subscription_plans() -> dict:
    """
    Crea i piani subscription su NOWPayments se non esistono già.
    Da chiamare una volta all'avvio.
    Ritorna { "pro": plan_id, "elite": plan_id }
    """
    global _SUBSCRIPTION_PLAN_IDS

    # Prova a leggere da env (se già creati)
    pro_id   = os.getenv("NOWPAY_PLAN_PRO", "")
    elite_id = os.getenv("NOWPAY_PLAN_ELITE", "")

    if pro_id and elite_id:
        _SUBSCRIPTION_PLAN_IDS = {"pro": pro_id, "elite": elite_id}
        logger.info("Subscription plans caricati da env: pro=%s elite=%s", pro_id, elite_id)
        return _SUBSCRIPTION_PLAN_IDS

    # Crea i piani
    for plan_key, plan_cfg in PLANS.items():
        try:
            result = _request("POST", "/subscriptions/plans", {
                "title":            f"FundShot {plan_cfg['name']} — Monthly",
                "interval_day":     30,
                "amount":           plan_cfg["recurring"],
                "currency":         "usd",
                "ipn_callback_url": "https://api.fundshot.app/api/payments/webhook",
                "success_url":      "https://fundshot.app?payment=success",
                "cancel_url":       "https://fundshot.app?payment=cancelled",
            })
            plan_id = str(result.get("id", ""))
            if plan_id:
                _SUBSCRIPTION_PLAN_IDS[plan_key] = plan_id
                logger.info("Subscription plan creato: %s → id=%s", plan_key, plan_id)
        except Exception as e:
            logger.error("setup_subscription_plans %s: %s", plan_key, e)

    return _SUBSCRIPTION_PLAN_IDS


def create_subscription(
    email: str,
    plan: str,
) -> dict:
    """
    Aggiunge un subscriber al piano subscription NOWPayments.
    NOWPayments invierà un'email con il link di pagamento.
    Ritorna { subscription_id, payment_url }
    """
    plan_id = _SUBSCRIPTION_PLAN_IDS.get(plan)
    if not plan_id:
        raise ValueError(f"Subscription plan ID non trovato per {plan}. Chiamare setup_subscription_plans() prima.")

    result = _request("POST", "/subscriptions", {
        "subscription_plan_id": plan_id,
        "email":               email,
    })
    return {
        "subscription_id": str(result.get("id", "")),
        "payment_url":     result.get("payment_url", ""),
        "status":          result.get("status", "pending"),
    }


def get_subscription_plans() -> dict:
    """Ritorna i plan IDs caricati."""
    return _SUBSCRIPTION_PLAN_IDS.copy()
