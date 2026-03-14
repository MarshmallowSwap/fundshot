"""
onboarding.py — FundShot SaaS
Multi-exchange onboarding wizard (Bybit, Binance, OKX).

Flow:
  /start → overview exchanges → choose exchange → demo/live
         → API key → API secret [→ passphrase if OKX] → test connection ✅

Credentials are AES-256 encrypted and saved to Supabase.
"""

import logging
from dotenv import load_dotenv
load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from db.supabase_client import (
    get_or_create_user,
    get_user,
    save_credentials,
    get_credentials,
    delete_credentials,
    update_user_exchanges,
)
from exchanges import make_client, SUPPORTED_EXCHANGES

logger = logging.getLogger(__name__)

# ── States ────────────────────────────────────────────────────────────────────
(
    ST_MAIN,
    ST_CHOOSE_EXCHANGE,
    ST_CHOOSE_ENV,
    ST_WAIT_KEY,
    ST_WAIT_SECRET,
    ST_WAIT_PASSPHRASE,
) = range(6)

_EX     = "onb_exchange"
_ENV    = "onb_environment"
_KEY    = "onb_api_key"
_SECRET = "onb_api_secret"

# ── Exchange metadata ─────────────────────────────────────────────────────────
EXCHANGE_META = {
    "bybit":       {"emoji": "🟡", "name": "Bybit",       "needs_passphrase": False},
    "binance":     {"emoji": "🟠", "name": "Binance",     "needs_passphrase": False},
    "okx":         {"emoji": "🔵", "name": "OKX",         "needs_passphrase": True},
    "hyperliquid": {"emoji": "🟣", "name": "Hyperliquid", "needs_passphrase": False},
}


def _mask(s: str) -> str:
    if not s or len(s) < 8:
        return "***"
    return s[:4] + "..." + s[-4:]


# ── Keyboards ─────────────────────────────────────────────────────────────────

def _kb_main(configured: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("➕ Add Exchange", callback_data="onb_add")]]
    for ex in configured:
        meta  = EXCHANGE_META.get(ex, {})
        label = f"🗑 Remove {meta.get('emoji','')} {meta.get('name', ex.capitalize())}"
        rows.append([InlineKeyboardButton(label, callback_data=f"onb_del_{ex}")])
    rows.append([InlineKeyboardButton("❌ Close", callback_data="onb_close")])
    return InlineKeyboardMarkup(rows)



async def _get_user_plan(chat_id: int) -> str:
    """Legge il piano utente da Supabase (con check scadenza)."""
    try:
        from db.supabase_client import get_user, get_client
        from datetime import datetime, timezone
        user = await get_user(chat_id)
        if not user or user.plan == "free":
            return "free"
        db  = get_client()
        res = db.table("users").select("plan_expires_at").eq("id", user.id).single().execute()
        exp = (res.data or {}).get("plan_expires_at")
        if exp:
            exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > exp_dt:
                return "free"
        return user.plan
    except Exception:
        return "free"


def _kb_exchanges(configured: list, plan: str = "free") -> InlineKeyboardMarkup:
    buttons = []
    # Pro: 1 exchange max — se ne ha già uno, blocca gli altri
    pro_limit_reached = (plan == "pro" and len(configured) >= 1)

    for ex in ["bybit", "binance", "okx", "hyperliquid"]:
        meta    = EXCHANGE_META.get(ex, {})
        enabled = ex in SUPPORTED_EXCHANGES
        already = ex in configured
        if already:
            buttons.append([
                InlineKeyboardButton(
                    f"✅ {meta['emoji']} {meta['name']}",
                    callback_data=f"onb_ex_{ex}",
                ),
                InlineKeyboardButton("🗑", callback_data=f"onb_del_{ex}"),
            ])
        elif pro_limit_reached and enabled:
            # Pro ha già un exchange — mostra bloccato
            buttons.append([InlineKeyboardButton(
                f"🔒 {meta['emoji']} {meta['name']} — Elite only",
                callback_data="onb_pro_limit",
            )])
        elif enabled:
            buttons.append([InlineKeyboardButton(
                f"{meta['emoji']} {meta['name']}",
                callback_data=f"onb_ex_{ex}",
            )])
        else:
            buttons.append([InlineKeyboardButton(
                f"🔜 {meta['name']} (coming soon)",
                callback_data="onb_coming_soon",
            )])
    buttons.append([InlineKeyboardButton("❌ Close", callback_data="onb_close")])
    return InlineKeyboardMarkup(buttons)


def _kb_environment() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 Demo (recommended to start)", callback_data="onb_env_demo")],
        [InlineKeyboardButton("💰 Live (real funds)", callback_data="onb_env_live")],
        [InlineKeyboardButton("⬅️ Back", callback_data="onb_back_exchange")],
    ])


# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    handle  = update.effective_user.username or ""

    user       = await get_or_create_user(chat_id, handle)
    configured = user.active_exchanges if user else []

    if configured:
        lines = []
        for ex in configured:
            cred = await get_credentials(user.id, ex)
            if cred:
                meta      = EXCHANGE_META.get(ex, {})
                env_label = "Demo 🧪" if cred.environment == "demo" else "Live 💰"
                lines.append(
                    f"{meta.get('emoji','')} *{meta.get('name', ex.capitalize())}* "
                    f"({env_label}) — `{_mask(cred.api_key)}`"
                )
        header = (
            "🤖 *FundShot Bot* — Active ✅\n\n"
            f"👤 Chat ID: `{chat_id}`\n\n"
            "*Configured exchanges:*\n" + "\n".join(lines) + "\n\n"
            "Select an exchange to add or remove:"
        )
    else:
        header = (
            "🤖 *Welcome to FundShot!*\n\n"
            "Monitor 500+ perpetual pairs across multiple exchanges, "
            "receive smart funding rate alerts and automate your strategy.\n\n"
            "🏦 *Choose the exchange to configure:*"
        )

    # Apre direttamente la scelta exchange
    plan = await _get_user_plan(chat_id)
    await update.message.reply_text(
        header,
        parse_mode="Markdown",
        reply_markup=_kb_exchanges(configured, plan),
    )
    return ST_CHOOSE_EXCHANGE


# ── Main callback ─────────────────────────────────────────────────────────────

async def main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "onb_add":
        chat_id    = update.effective_chat.id
        user       = await get_user(chat_id)
        configured = user.active_exchanges if user else []
        plan       = await _get_user_plan(chat_id)
        await query.edit_message_text(
            "🏦 *Choose the exchange to configure:*",
            parse_mode="Markdown",
            reply_markup=_kb_exchanges(configured, plan),
        )
        return ST_CHOOSE_EXCHANGE

    if data == "onb_coming_soon":
        await query.answer("🚧 Coming soon!", show_alert=True)
        return ST_CHOOSE_EXCHANGE

    if data == "onb_pro_limit":
        await query.answer(
            "Pro plan supports 1 exchange. Upgrade to Elite for multi-exchange.",
            show_alert=True,
        )
        return ST_CHOOSE_EXCHANGE

    if data.startswith("onb_del_"):
        ex      = data.replace("onb_del_", "")
        chat_id = update.effective_chat.id
        user    = await get_user(chat_id)
        if user:
            await delete_credentials(user.id, ex)
            active = [e for e in user.active_exchanges if e != ex]
            await update_user_exchanges(user.id, active)
            configured = active
        else:
            configured = []
        meta = EXCHANGE_META.get(ex, {})
        await query.edit_message_text(
            f"🗑 *{meta.get('name', ex.capitalize())}* credentials removed.\n\n"
            "🏦 *Choose the exchange to configure:*",
            parse_mode="Markdown",
            reply_markup=_kb_exchanges(configured),
        )
        return ST_CHOOSE_EXCHANGE

    if data == "onb_close":
        await query.edit_message_text("✅ Setup closed. Use /start to reopen.")
        return ConversationHandler.END

    if data == "onb_back_main":
        chat_id    = update.effective_chat.id
        user       = await get_user(chat_id)
        configured = user.active_exchanges if user else []
        plan       = await _get_user_plan(chat_id)
        await query.edit_message_text(
            "🏦 *Choose the exchange to configure:*",
            parse_mode="Markdown",
            reply_markup=_kb_exchanges(configured, plan),
        )
        return ST_CHOOSE_EXCHANGE

    return ST_CHOOSE_EXCHANGE


# ── Exchange choice ───────────────────────────────────────────────────────────

async def exchange_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "onb_back_main":
        chat_id    = update.effective_chat.id
        user       = await get_user(chat_id)
        configured = user.active_exchanges if user else []
        plan       = await _get_user_plan(chat_id)
        await query.edit_message_text(
            "🏦 *Choose the exchange to configure:*",
            parse_mode="Markdown",
            reply_markup=_kb_exchanges(configured, plan),
        )
        return ST_CHOOSE_EXCHANGE

    if data.startswith("onb_ex_"):
        exchange = data.replace("onb_ex_", "")
        context.user_data[_EX] = exchange
        meta = EXCHANGE_META.get(exchange, {})
        await query.edit_message_text(
            f"{meta.get('emoji','')} *{meta.get('name', exchange.capitalize())}* selected.\n\n"
            "Would you like to use *Demo* or *Live* keys?\n\n"
            "⚠️ With *Live* you trade with real funds — use keys with "
            "trading permissions only (no withdrawal).",
            parse_mode="Markdown",
            reply_markup=_kb_environment(),
        )
        return ST_CHOOSE_ENV

    return ST_CHOOSE_EXCHANGE


# ── Environment choice ────────────────────────────────────────────────────────

async def environment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "onb_back_exchange":
        chat_id    = update.effective_chat.id
        user       = await get_user(chat_id)
        configured = user.active_exchanges if user else []
        plan       = await _get_user_plan(chat_id)
        await query.edit_message_text(
            "🏦 *Choose the exchange to configure:*",
            parse_mode="Markdown",
            reply_markup=_kb_exchanges(configured, plan),
        )
        return ST_CHOOSE_EXCHANGE

    if data in ("onb_env_demo", "onb_env_live"):
        env     = "demo" if data == "onb_env_demo" else "live"
        context.user_data[_ENV] = env
        ex      = context.user_data.get(_EX, "bybit")
        meta    = EXCHANGE_META.get(ex, {})
        env_lbl = "Demo 🧪" if env == "demo" else "Live 💰"
        await query.edit_message_text(
            f"🔑 *{meta.get('name', ex.capitalize())} — {env_lbl}*\n\n"
            "Send your *API Key*:\n"
            "_(message will be deleted immediately for security)_",
            parse_mode="Markdown",
        )
        return ST_WAIT_KEY

    return ST_CHOOSE_ENV


# ── Receive API Key ───────────────────────────────────────────────────────────

async def receive_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    context.user_data[_KEY] = value
    ex   = context.user_data.get(_EX, "bybit")
    meta = EXCHANGE_META.get(ex, {})
    await update.message.reply_text(
        f"✅ API Key received: `{_mask(value)}`\n\n"
        f"🔒 Now send your *{meta.get('name', ex.capitalize())} API Secret*:\n"
        "_(will be deleted immediately)_",
        parse_mode="Markdown",
    )
    return ST_WAIT_SECRET


# ── Receive API Secret ────────────────────────────────────────────────────────

async def receive_api_secret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    context.user_data[_SECRET] = value
    ex   = context.user_data.get(_EX, "bybit")
    meta = EXCHANGE_META.get(ex, {})

    if meta.get("needs_passphrase"):
        await update.message.reply_text(
            f"✅ API Secret received: `{_mask(value)}`\n\n"
            "🔑 *OKX also requires a Passphrase.*\n"
            "Send your *API Passphrase*:\n"
            "_(the one you set when creating the API key)_",
            parse_mode="Markdown",
        )
        return ST_WAIT_PASSPHRASE

    return await _finalize(update, context, passphrase="")


# ── Receive Passphrase (OKX only) ─────────────────────────────────────────────

async def receive_passphrase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    return await _finalize(update, context, passphrase=value)


# ── Finalize: save + test ─────────────────────────────────────────────────────

async def _finalize(update, context, passphrase: str) -> int:
    chat_id     = update.effective_chat.id
    api_key     = context.user_data.get(_KEY, "")
    api_secret  = context.user_data.get(_SECRET, "")
    exchange    = context.user_data.get(_EX, "bybit")
    environment = context.user_data.get(_ENV, "demo")
    meta        = EXCHANGE_META.get(exchange, {})

    user = await get_or_create_user(chat_id)
    ok   = await save_credentials(
        user_id=user.id,
        exchange=exchange,
        api_key=api_key,
        api_secret=api_secret,
        environment=environment,
        passphrase=passphrase,
    )

    if not ok:
        await update.message.reply_text("❌ Error saving credentials. Try again with /start.")
        return ConversationHandler.END

    active = list(set(user.active_exchanges + [exchange]))
    await update_user_exchanges(user.id, active)

    # Test connection
    try:
        kwargs = {"passphrase": passphrase} if passphrase else {}
        client = make_client(
            exchange=exchange,
            api_key=api_key,
            api_secret=api_secret,
            demo=(environment == "demo"),
            testnet=False,
            **kwargs,
        )
        result = await client.test_connection()
        auth   = result.get("auth", {})
        if auth.get("ok"):
            equity      = auth.get("equity", 0)
            conn_status = f"✅ Connected — Equity: `${equity:,.2f}`"
        else:
            err         = auth.get("error", "unknown error")
            conn_status = f"⚠️ Connection failed: {err}\n_Check your keys and try again._"
    except Exception as e:
        conn_status = f"⚠️ Connection error: {e}"

    env_label = "Demo 🧪" if environment == "demo" else "Live 💰"
    await update.message.reply_text(
        f"🎉 *{meta.get('name', exchange.capitalize())} configured!*\n\n"
        f"{meta.get('emoji','')} Exchange: *{meta.get('name', exchange.capitalize())}*\n"
        f"🌍 Environment: *{env_label}*\n"
        f"🔑 API Key: `{_mask(api_key)}`\n\n"
        f"{conn_status}\n\n"
        "FundShot will now monitor funding rates for you.\n"
        "Use /help to see all available commands.\n\n"
        "_Add more exchanges anytime with /start._",
        parse_mode="Markdown",
    )

    for k in (_KEY, _SECRET, _EX, _ENV):
        context.user_data.pop(k, None)

    return ConversationHandler.END


# ── /deletekeys ───────────────────────────────────────────────────────────────

async def cmd_deletekeys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user    = await get_user(chat_id)
    if not user:
        await update.message.reply_text("No credentials found.")
        return
    for ex in user.active_exchanges:
        await delete_credentials(user.id, ex)
    await update_user_exchanges(user.id, [])
    await update.message.reply_text(
        "🗑 All credentials have been deleted.\n"
        "Use /start to reconfigure.",
    )


# ── Build ConversationHandler ─────────────────────────────────────────────────

def build_onboarding_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ST_MAIN:            [CallbackQueryHandler(main_callback,      pattern="^onb_")],
            ST_CHOOSE_EXCHANGE: [CallbackQueryHandler(exchange_callback,   pattern="^onb_")],
            ST_CHOOSE_ENV:      [CallbackQueryHandler(environment_callback, pattern="^onb_")],
            ST_WAIT_KEY:        [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_key)],
            ST_WAIT_SECRET:     [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_secret)],
            ST_WAIT_PASSPHRASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_passphrase)],
        },
        fallbacks=[CommandHandler("start", start)],
        per_chat=True,
    )
