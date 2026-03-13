"""
onboarding.py — Funding King SaaS
Wizard Telegram per registrazione utente multi-tenant.

Flusso:
  /start → scelta exchange → demo/live → API key → API secret → ✅

Le credenziali vengono cifrate AES-256 e salvate su Supabase.
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

# ── Stati ConversationHandler ─────────────────────────────────────────────────
(
    ST_MAIN,
    ST_CHOOSE_EXCHANGE,
    ST_CHOOSE_ENV,
    ST_WAIT_KEY,
    ST_WAIT_SECRET,
) = range(5)

# Chiavi context.user_data
_EX  = "onb_exchange"
_ENV = "onb_environment"


def _mask(s: str) -> str:
    if not s or len(s) < 8:
        return "***"
    return s[:4] + "..." + s[-4:]


# ── Keyboard helpers ──────────────────────────────────────────────────────────

def _kb_main(has_bybit: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("➕ Aggiungi Exchange", callback_data="onb_add")],
    ]
    if has_bybit:
        rows.append([InlineKeyboardButton("🗑 Rimuovi Bybit", callback_data="onb_del_bybit")])
    rows.append([InlineKeyboardButton("❌ Chiudi", callback_data="onb_close")])
    return InlineKeyboardMarkup(rows)


def _kb_exchanges() -> InlineKeyboardMarkup:
    buttons = []
    labels = {
        "bybit":       "🟡 Bybit",
        "binance":     "🟡 Binance  (presto)",
        "okx":         "🟡 OKX  (presto)",
        "hyperliquid": "🟡 Hyperliquid  (presto)",
    }
    all_ex = ["bybit", "binance", "okx", "hyperliquid"]
    for ex in all_ex:
        enabled = ex in SUPPORTED_EXCHANGES
        buttons.append([InlineKeyboardButton(
            labels.get(ex, ex),
            callback_data=f"onb_ex_{ex}" if enabled else "onb_coming_soon",
        )])
    buttons.append([InlineKeyboardButton("⬅️ Indietro", callback_data="onb_back_main")])
    return InlineKeyboardMarkup(buttons)


def _kb_environment() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 Demo (consigliato per iniziare)", callback_data="onb_env_demo")],
        [InlineKeyboardButton("💰 Live (fondi reali)", callback_data="onb_env_live")],
        [InlineKeyboardButton("⬅️ Indietro", callback_data="onb_back_exchange")],
    ])


# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    handle  = update.effective_user.username or ""

    user = await get_or_create_user(chat_id, handle)
    cred = await get_credentials(user.id, "bybit")
    has_bybit = cred is not None

    if has_bybit:
        env_label = "Demo" if cred.environment == "demo" else "💰 Live"
        text = (
            "👑 *Funding King Bot* — Attivo ✅\n\n"
            f"👤 Chat ID: `{chat_id}`\n"
            f"🏦 Bybit ({env_label}): `{_mask(cred.api_key)}`\n\n"
            "Usa /help per vedere tutti i comandi.\n"
            "Vuoi aggiungere un altro exchange o gestire le chiavi?"
        )
    else:
        text = (
            "👑 *Funding King Bot — Benvenuto!*\n\n"
            "Monitora 500+ coppie perpetual, ricevi alert intelligenti "
            "e automatizza il trading sul funding rate.\n\n"
            "Per iniziare, configura le tue API key:"
        )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=_kb_main(has_bybit),
    )
    return ST_MAIN


# ── Callback principale ───────────────────────────────────────────────────────

async def main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "onb_add":
        await query.edit_message_text(
            "🏦 *Scegli l'exchange da configurare:*",
            parse_mode="Markdown",
            reply_markup=_kb_exchanges(),
        )
        return ST_CHOOSE_EXCHANGE

    if data == "onb_coming_soon":
        await query.answer("🚧 Presto disponibile!", show_alert=True)
        return ST_MAIN

    if data.startswith("onb_del_"):
        ex = data.replace("onb_del_", "")
        chat_id = update.effective_chat.id
        user = await get_user(chat_id)
        if user:
            await delete_credentials(user.id, ex)
            active = [e for e in user.active_exchanges if e != ex]
            await update_user_exchanges(user.id, active)
        await query.edit_message_text(
            f"🗑 Credenziali *{ex.capitalize()}* rimosse.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if data == "onb_close":
        await query.edit_message_text("✅ Setup chiuso. Usa /start per riaprirlo.")
        return ConversationHandler.END

    if data == "onb_back_main":
        chat_id = update.effective_chat.id
        user = await get_user(chat_id)
        cred = await get_credentials(user.id, "bybit") if user else None
        await query.edit_message_text(
            "👑 *Funding King Bot — Setup*\n\nCosa vuoi fare?",
            parse_mode="Markdown",
            reply_markup=_kb_main(cred is not None),
        )
        return ST_MAIN

    return ST_MAIN


# ── Scelta exchange ───────────────────────────────────────────────────────────

async def exchange_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "onb_back_main":
        await query.edit_message_text(
            "👑 *Funding King Bot — Setup*\n\nCosa vuoi fare?",
            parse_mode="Markdown",
            reply_markup=_kb_main(),
        )
        return ST_MAIN

    if data.startswith("onb_ex_"):
        exchange = data.replace("onb_ex_", "")
        context.user_data[_EX] = exchange
        await query.edit_message_text(
            f"🏦 *{exchange.capitalize()}* selezionato.\n\n"
            "Vuoi usare le chiavi *Demo* o *Live*?\n\n"
            "⚠️ Con *Live* operi con fondi reali — assicurati di usare "
            "chiavi con permessi solo per trading (non withdrawal).",
            parse_mode="Markdown",
            reply_markup=_kb_environment(),
        )
        return ST_CHOOSE_ENV

    return ST_CHOOSE_EXCHANGE


# ── Scelta ambiente ───────────────────────────────────────────────────────────

async def environment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "onb_back_exchange":
        await query.edit_message_text(
            "🏦 *Scegli l'exchange da configurare:*",
            parse_mode="Markdown",
            reply_markup=_kb_exchanges(),
        )
        return ST_CHOOSE_EXCHANGE

    if data in ("onb_env_demo", "onb_env_live"):
        env = "demo" if data == "onb_env_demo" else "live"
        context.user_data[_ENV] = env
        ex = context.user_data.get(_EX, "bybit")
        env_label = "Demo 🧪" if env == "demo" else "Live 💰"
        await query.edit_message_text(
            f"🔑 *{ex.capitalize()} — {env_label}*\n\n"
            "Invia la tua *API Key*:\n"
            "_(il messaggio sarà eliminato subito per sicurezza)_",
            parse_mode="Markdown",
        )
        return ST_WAIT_KEY

    return ST_CHOOSE_ENV


# ── Ricezione API Key ─────────────────────────────────────────────────────────

async def receive_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    value   = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    context.user_data["onb_api_key"] = value
    ex = context.user_data.get(_EX, "bybit")

    await update.message.reply_text(
        f"✅ API Key ricevuta: `{_mask(value)}`\n\n"
        f"🔒 Ora invia il tuo *{ex.capitalize()} API Secret*:\n"
        "_(sarà eliminato subito)_",
        parse_mode="Markdown",
    )
    return ST_WAIT_SECRET


# ── Ricezione API Secret ──────────────────────────────────────────────────────

async def receive_api_secret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    value   = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    api_key = context.user_data.get("onb_api_key", "")
    exchange = context.user_data.get(_EX, "bybit")
    environment = context.user_data.get(_ENV, "demo")

    # Salva su Supabase (cifrato AES-256)
    user = await get_or_create_user(chat_id)
    ok   = await save_credentials(
        user_id=user.id,
        exchange=exchange,
        api_key=api_key,
        api_secret=value,
        environment=environment,
    )

    if not ok:
        await update.message.reply_text(
            "❌ Errore nel salvataggio. Riprova con /start.",
        )
        return ConversationHandler.END

    # Aggiorna exchange attivi
    active = list(set(user.active_exchanges + [exchange]))
    await update_user_exchanges(user.id, active)

    # Test connessione
    conn_status = "⏳ Test connessione..."
    try:
        client = make_client(
            exchange=exchange,
            api_key=api_key,
            api_secret=value,
            demo=(environment == "demo"),
            testnet=False,
        )
        result = await client.test_connection()
        if result.get("auth", {}).get("ok"):
            equity = result["auth"].get("equity", 0)
            conn_status = f"✅ Connessione OK — Equity: `${equity:,.2f}`"
        else:
            err = result.get("auth", {}).get("error", "errore sconosciuto")
            conn_status = f"⚠️ Connessione fallita: {err}\n_Controlla le chiavi e riprova._"
    except Exception as e:
        conn_status = f"⚠️ Errore test: {e}"

    env_label = "Demo 🧪" if environment == "demo" else "Live 💰"
    await update.message.reply_text(
        f"🎉 *{exchange.capitalize()} configurato!*\n\n"
        f"🏦 Exchange: *{exchange.capitalize()}*\n"
        f"🌍 Ambiente: *{env_label}*\n"
        f"🔑 API Key: `{_mask(api_key)}`\n\n"
        f"{conn_status}\n\n"
        "Il bot inizierà a monitorare il funding rate per te.\n"
        "Usa /help per vedere tutti i comandi disponibili.",
        parse_mode="Markdown",
    )

    # Pulizia context
    for k in ("onb_api_key", _EX, _ENV):
        context.user_data.pop(k, None)

    return ConversationHandler.END


# ── /deletekeys ───────────────────────────────────────────────────────────────

async def cmd_deletekeys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = await get_user(chat_id)
    if not user:
        await update.message.reply_text("Nessuna credenziale trovata.")
        return
    for ex in user.active_exchanges:
        await delete_credentials(user.id, ex)
    await update_user_exchanges(user.id, [])
    await update.message.reply_text(
        "🗑 Tutte le credenziali sono state eliminate.\n"
        "Usa /start per configurare di nuovo.",
    )


# ── Registrazione ConversationHandler ────────────────────────────────────────

def build_onboarding_handler() -> ConversationHandler:
    """Restituisce il ConversationHandler da registrare in bot.py."""
    return ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ST_MAIN: [
                CallbackQueryHandler(main_callback, pattern="^onb_"),
            ],
            ST_CHOOSE_EXCHANGE: [
                CallbackQueryHandler(exchange_callback, pattern="^onb_"),
            ],
            ST_CHOOSE_ENV: [
                CallbackQueryHandler(environment_callback, pattern="^onb_"),
            ],
            ST_WAIT_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_key),
            ],
            ST_WAIT_SECRET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_secret),
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        per_chat=True,
    )
