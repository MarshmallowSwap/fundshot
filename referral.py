"""
referral.py — FundShot SaaS
Sistema referral + influencer.

Regole:
- Chiunque può generare un link referral con /referral
- Referrer guadagna 10% su ogni pagamento dell'invitato (a vita, inclusi rinnovi)
- Influencer (creati dall'admin) → il loro link dà 5% di sconto all'invitato
- Payout automatico mensile in USDT quando balance >= $5
- Admin: /addinf @username per promuovere un utente a influencer
"""

import logging
import os
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

REFERRAL_COMMISSION_PCT = 10.0   # 10% per tutti i referrer
INFLUENCER_DISCOUNT_PCT = 5.0    # 5% sconto per chi arriva da link influencer
PAYOUT_MIN_USD          = 5.0    # soglia minima payout
REFERRAL_REWARD_DAYS    = 30     # giorni gratis per referral utente normale


# ── DB helpers ────────────────────────────────────────────────────────────────

async def get_referral_info(user_id: str) -> dict:
    """
    Ritorna info complete sul programma referral di un utente:
    codice, tipo (user/influencer), invitati, guadagni, balance.
    """
    from db.supabase_client import get_client
    db = get_client()
    try:
        # Info utente
        u_res = db.table("users").select(
            "referral_code,is_influencer,referral_balance_usd,referral_total_earned_usd"
        ).eq("id", user_id).single().execute()
        u = u_res.data or {}

        # Referral fatti
        r_res = db.table("referrals").select("*").eq("referrer_user_id", user_id).execute()
        referrals = r_res.data or []

        pending   = sum(1 for r in referrals if r["status"] == "pending")
        converted = sum(1 for r in referrals if r["status"] in ("converted", "rewarded"))

        return {
            "code":          u.get("referral_code", ""),
            "is_influencer": u.get("is_influencer", False),
            "balance":       float(u.get("referral_balance_usd", 0) or 0),
            "total_earned":  float(u.get("referral_total_earned_usd", 0) or 0),
            "total_invited": len(referrals),
            "pending":       pending,
            "converted":     converted,
        }
    except Exception as e:
        logger.error("get_referral_info: %s", e)
        return {"code": "", "is_influencer": False, "balance": 0, "total_earned": 0,
                "total_invited": 0, "pending": 0, "converted": 0}


async def add_commission(payment_user_id: str, amount_usd: float) -> float:
    """
    Calcola e accredita la commissione al referrer quando il payment_user paga.
    Ritorna la commissione accreditata (0 se nessun referrer).
    """
    from db.supabase_client import get_client
    db = get_client()
    try:
        # Trova il referrer di questo utente
        ref_res = db.table("referrals").select(
            "referrer_user_id,status"
        ).eq("referred_user_id", payment_user_id).execute()

        if not ref_res.data:
            return 0.0

        ref = ref_res.data[0]
        referrer_id = ref["referrer_user_id"]
        commission  = round(amount_usd * REFERRAL_COMMISSION_PCT / 100, 4)

        # Aggiorna balance referrer
        curr = db.table("users").select(
            "referral_balance_usd,referral_total_earned_usd"
        ).eq("id", referrer_id).single().execute()
        d = curr.data or {}

        new_balance = float(d.get("referral_balance_usd", 0) or 0) + commission
        new_total   = float(d.get("referral_total_earned_usd", 0) or 0) + commission

        db.table("users").update({
            "referral_balance_usd":       round(new_balance, 4),
            "referral_total_earned_usd":  round(new_total, 4),
        }).eq("id", referrer_id).execute()

        # Aggiorna status referral → converted/rewarded
        if ref["status"] == "pending":
            db.table("referrals").update({
                "status":       "converted",
                "converted_at": datetime.now(timezone.utc).isoformat(),
                "reward_days":  0,  # non si usano reward_days nel sistema cash
            }).eq("referred_user_id", payment_user_id).execute()

        logger.info("Commissione %.4f USDT accreditata a %s", commission, referrer_id)
        return commission

    except Exception as e:
        logger.error("add_commission: %s", e)
        return 0.0


async def get_discount_for_user(user_id: str) -> float:
    """
    Ritorna la percentuale di sconto permanente per un utente
    (5% se si è registrato tramite link influencer, 0 altrimenti).
    """
    from db.supabase_client import get_client
    db = get_client()
    try:
        res = db.table("users").select("referral_discount_pct").eq("id", user_id).single().execute()
        return float((res.data or {}).get("referral_discount_pct", 0) or 0)
    except Exception:
        return 0.0


def apply_discount(amount_usd: float, discount_pct: float) -> float:
    """Applica lo sconto a un importo."""
    if discount_pct <= 0:
        return amount_usd
    return round(amount_usd * (1 - discount_pct / 100), 2)


async def process_monthly_payouts(bot) -> int:
    """
    Job mensile: invia payout USDT a tutti i referrer con balance >= PAYOUT_MIN_USD.
    Ritorna il numero di payout effettuati.
    """
    from db.supabase_client import get_client
    db = get_client()
    count = 0
    try:
        # Trova tutti gli utenti con balance >= soglia
        res = db.table("users").select(
            "id,chat_id,telegram_handle,referral_balance_usd,referral_wallet_usdt"
        ).gte("referral_balance_usd", PAYOUT_MIN_USD).execute()

        users = res.data or []
        logger.info("Monthly payout: %d utenti con balance >= $%.2f", len(users), PAYOUT_MIN_USD)

        for u in users:
            try:
                balance = float(u.get("referral_balance_usd", 0) or 0)
                wallet  = u.get("referral_wallet_usdt", "")
                chat_id = u.get("chat_id")

                if not wallet:
                    # Notifica che manca il wallet
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"💰 *Referral Payout Available!*\n\n"
                            f"You have `${balance:.2f} USDT` ready to withdraw.\n\n"
                            f"To receive your payout, set your USDT (TRC20) wallet with:\n"
                            f"`/setwallet YOUR_USDT_ADDRESS`"
                        ),
                        parse_mode="Markdown",
                    )
                    continue

                # TODO: integrazione mass payout NOWPayments
                # Per ora: notifica manuale + azzera balance
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"💸 *Referral Payout Sent!*\n\n"
                        f"Amount: `${balance:.2f} USDT`\n"
                        f"Wallet: `{wallet[:6]}...{wallet[-4:]}`\n\n"
                        f"Payment processing — arrives within 24h.\n"
                        f"_Next payout: 1st of next month_"
                    ),
                    parse_mode="Markdown",
                )

                # Azzera balance (mantieni total_earned)
                db.table("users").update({
                    "referral_balance_usd": 0.0,
                }).eq("id", u["id"]).execute()

                count += 1

            except Exception as e:
                logger.error("payout utente %s: %s", u.get("chat_id"), e)

    except Exception as e:
        logger.error("process_monthly_payouts: %s", e)

    return count
