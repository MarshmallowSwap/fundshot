"""
commands.py — FundShot Bot
Tutti i command handler Telegram + setup wizard.
"""

import os
import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ_IT = ZoneInfo("Europe/Rome")

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

import bybit_client as bc
import alert_logic as al
import watchlist_manager as wm
import backtester as bt
import funding_tracker as ft
from bybit_client import close_positions_by_mm, close_positions_by_pnl
import user_store
import session_manager
from user_registry import registry as _registry

logger = logging.getLogger(__name__)

# ── Helper multi-exchange ─────────────────────────────────────────────────────

EXCHANGE_EMOJI = {"bybit": "🟡", "binance": "🟠", "okx": "🔵", "hyperliquid": "🟣"}
EXCHANGE_NAME  = {"bybit": "Bybit", "binance": "Binance", "okx": "OKX", "hyperliquid": "Hyperliquid"}

# ── Plan gate ─────────────────────────────────────────────────────────────────

async def _get_user_plan(chat_id) -> str:
    """Ritorna il piano attuale dell'utente (free/pro/elite)."""
    try:
        from db.supabase_client import get_user, get_client
        from datetime import datetime, timezone
        user = await get_user(chat_id)
        if not user:
            return "free"
        if user.plan == "free":
            return "free"
        # Verifica scadenza
        db  = get_client()
        res = db.table("users").select("plan_expires_at").eq("id", user.id).single().execute()
        exp = (res.data or {}).get("plan_expires_at")
        if exp:
            exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > exp_dt:
                return "free"  # scaduto
        return user.plan
    except Exception:
        return "free"


async def _require_plan(update, min_plan: str = "pro") -> bool:
    """
    Verifica che l'utente abbia il piano minimo richiesto.
    Ritorna True se OK, False e invia messaggio se non autorizzato.
    """
    chat_id = update.effective_chat.id
    plan    = await _get_user_plan(chat_id)
    order   = {"free": 0, "pro": 1, "elite": 2}
    if order.get(plan, 0) >= order.get(min_plan, 1):
        return True

    plan_labels = {"pro": "⚡ Pro", "elite": "👑 Elite"}
    required    = plan_labels.get(min_plan, min_plan.capitalize())
    prices      = {"pro": "$20/mo", "elite": "$45/mo (or $300 lifetime)"}
    price       = prices.get(min_plan, "")

    await update.message.reply_text(
        f"\U0001F512 *{required} required*\n\n"
        f"This feature is available from the *{required}* plan ({price}).\n\n"
        f"Your current plan: *{plan.capitalize()}*\n\n"
        "Use /upgrade to unlock all features.",
        parse_mode="Markdown",
    )
    return False



def _user_exchanges(chat_id) -> list:
    """Lista degli exchange configurati per questo utente (da registry)."""
    ucs = [uc for uc in _registry.all_clients() if str(uc.chat_id) == str(chat_id)]
    return [uc.exchange for uc in ucs]

def _get_client(chat_id, exchange: str = None):
    """
    Restituisce il client per (chat_id, exchange).
    Se exchange è None, usa il primo disponibile (o bybit come fallback).
    """
    exchanges = _user_exchanges(chat_id)
    if not exchanges:
        return None, None
    target = exchange if exchange in exchanges else exchanges[0]
    client = _registry.get_client(int(chat_id), target)
    return client, target

# ── ConversationHandler states ────────────────────────────────────────────────
MENU, WAITING_API_KEY, WAITING_API_SECRET = range(3)


def is_watched(symbol: str) -> bool:
    """Proxy verso watchlist_manager — usato da bot.py."""
    return wm.is_watched(symbol)


# ══════════════════════════════════════════════════════════════════════════════
# /start — Setup Wizard
# ══════════════════════════════════════════════════════════════════════════════

def _has_credentials(chat_id: int | str | None = None) -> bool:
    """Verifica credenziali: per-utente se chat_id è fornito, globale come fallback."""
    if chat_id is not None:
        return user_store.has_credentials(chat_id)
    # Fallback legacy: controlla variabili d'ambiente globali
    return bool(os.getenv("BYBIT_API_KEY")) and bool(os.getenv("BYBIT_API_SECRET"))


def _build_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Imposta API Key", callback_data="set_api_key")],
        [InlineKeyboardButton("🔒 Imposta API Secret", callback_data="set_api_secret")],
        [InlineKeyboardButton("✅ Conferma e Avvia", callback_data="confirm_start")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id

    if _has_credentials(chat_id):
        key_masked = _mask(user_store.get_api_key(chat_id))
        await update.message.reply_text(
            "🤖 *FundShot Bot* — Attivo ✅\n\n"
            f"Chat ID: `{chat_id}`\n"
            f"API Key: `{key_masked}`\n\n"
            "Usa /help per vedere tutti i comandi.\n"
            "Usa /deletekeys per rimuovere le tue credenziali.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    key    = _mask(user_store.get_api_key(chat_id))
    secret = _mask(user_store.get_api_secret(chat_id))
    text = (
        "🤖 *FundShot Bot — Setup*\n\n"
        f"Chat ID: `{chat_id}` ✅ (rilevato automaticamente)\n"
        f"API Key: `{key or '⚠️ non impostata'}`\n"
        f"API Secret: `{secret or '⚠️ non impostato'}`\n\n"
        "Seleziona cosa configurare:"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=_build_menu_keyboard())
    return MENU


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    chat_id = update.effective_chat.id
    await query.answer()
    data = query.data

    if data == "set_api_key":
        await query.edit_message_text(
            "🔑 Invia la tua *Bybit API Key* (il messaggio verrà eliminato automaticamente):",
            parse_mode="Markdown",
        )
        return WAITING_API_KEY

    if data == "set_api_secret":
        await query.edit_message_text(
            "🔒 Invia il tuo *Bybit API Secret* (il messaggio verrà eliminato automaticamente):",
            parse_mode="Markdown",
        )
        return WAITING_API_SECRET

    if data == "confirm_start":
        chat_id = query.from_user.id
        if not _has_credentials(chat_id):
            await query.edit_message_text(
                "⚠️ Configura prima API Key e API Secret.",
                reply_markup=_build_menu_keyboard(),
            )
            return MENU
        session_manager.reload_session(chat_id)
        # Test connessione
        try:
            sess = session_manager.get_session(chat_id)
            test = await sess.test_connection()
            conn_status = "✅ Connessione Bybit OK" if test.get("ok") else f"⚠️ {test.get('error','errore')}"
        except Exception as e:
            conn_status = f"⚠️ Errore test: {e}"
        await query.edit_message_text(
            "✅ *Configurazione completata!*\n\n"
            f"API Key: `{_mask(user_store.get_api_key(chat_id))}`\n"
            f"API Secret: `{_mask(user_store.get_api_secret(chat_id))}`\n\n"
            f"{conn_status}\n\n"
            "Il bot inizia il monitoraggio. Usa /help per i comandi.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    return MENU


async def receive_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    value = update.message.text.strip()
    chat_id = update.effective_chat.id
    try:
        await update.message.delete()
    except Exception:
        pass
    user_store.set_key(chat_id, "api_key", value)
    session_manager.reload_session(chat_id)
    await update.message.reply_text(
        f"✅ API Key salvata: `{_mask(value)}`",
        parse_mode="Markdown",
        reply_markup=_build_menu_keyboard(),
    )
    return MENU


async def receive_api_secret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    value = update.message.text.strip()
    chat_id = update.effective_chat.id
    try:
        await update.message.delete()
    except Exception:
        pass
    user_store.set_key(chat_id, "api_secret", value)
    session_manager.reload_session(chat_id)
    await update.message.reply_text(
        f"✅ API Secret salvato: `{_mask(value)}`",
        parse_mode="Markdown",
        reply_markup=_build_menu_keyboard(),
    )
    return MENU


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Setup annullato. Usa /start per ricominciare.")
    return ConversationHandler.END


async def deletekeys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not user_store.has_credentials(chat_id):
        await update.message.reply_text("Non hai credenziali salvate.")
        return
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Elimina", callback_data="deletekeys_confirm"),
            InlineKeyboardButton("Annulla", callback_data="deletekeys_cancel"),
        ]
    ])
    await update.message.reply_text(
        "Sei sicuro di voler eliminare le tue credenziali Bybit?",
        reply_markup=keyboard,
    )


async def deletekeys_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    await query.answer()
    if query.data == "deletekeys_confirm":
        user_store.remove_user(chat_id)
        session_manager.remove_session(chat_id)
        await query.edit_message_text("Credenziali eliminate. Usa /start per riconfigurare.")
    else:
        await query.edit_message_text("Annullato. Credenziali al sicuro.")


# ══════════════════════════════════════════════════════════════════════════════
# /help
# ══════════════════════════════════════════════════════════════════════════════

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from db.supabase_client import get_user
    chat_id = update.effective_chat.id
    user    = await get_user(chat_id)
    plan    = user.plan if user else "free"

    # Verifica scadenza
    if plan != "free":
        try:
            from db.supabase_client import get_client
            from datetime import datetime, timezone
            db  = get_client()
            res = db.table("users").select("plan_expires_at").eq("id", user.id).single().execute()
            exp = (res.data or {}).get("plan_expires_at")
            if exp:
                exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) > exp_dt:
                    plan = "free"
        except Exception:
            pass

    plan_emoji = {"free": "🆓", "pro": "⚡", "elite": "👑"}.get(plan, "🆓")

    text = (
        f"⚡ *FundShot Bot — Commands*\n"
        f"Your plan: {plan_emoji} *{plan.capitalize()}*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"

        "📊 *FUNDING RATES*\n"
        "/top10 — Top 10 SHORT + LONG rates\n"
        "/storico `SYMBOL` — Last 8 funding cycles\n"
        "/storico `SYMBOL 7g` — 7-day history + stats\n"
        "/backtest `SYMBOL` — Simulate 30-day P&L ⚡\n\n"

        "━━━━━━━━━━━━━━━━━━━━━\n"
        "💼 *ACCOUNT*\n"
        "/saldo — Wallet balance per exchange\n"
        "/posizioni — Open positions with PnL\n\n"

        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 *AUTO-TRADING* ⚡\n"
        "/autotrader — Toggle auto-trader on/off\n\n"

        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 *WATCHLIST*\n"
        "/watchlist — Full watchlist status\n"
        "/watch `BTC ETH SOL` — Add symbols\n"
        "/unwatch `SYM` — Remove | `/unwatch all` reset\n"
        "/mute `SYM` — Mute alerts for symbol\n"
        "/unmute `SYM` — Reactivate alerts\n\n"

        "━━━━━━━━━━━━━━━━━━━━━\n"
        "💳 *SUBSCRIPTION*\n"
        "/plan — Your plan, expiry and billing\n"
        "/upgrade — Upgrade to Pro or Elite\n"
        "/referral — Your referral link + earnings\n"
        "/setwallet — Set USDT wallet for payouts\n\n"

        "━━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ *SETTINGS*\n"
        "/start — Configure exchange API keys\n"
        "/deletekeys — Remove your API keys\n\n"

        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🔔 *Alert levels:*\n"
        "🎰 JACKPOT ≥ ±3% | 🔴 HARD ≥ ±2%\n"
        "🔥 EXTREME ≥ ±1.5% | 🚨 HIGH ≥ ±1%\n"
        "📊 SOFT ≥ ±0.1% | ⏰ Pre-settlement ⚡\n\n"
        "_⚡ = Pro/Elite only_"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# /status
# ══════════════════════════════════════════════════════════════════════════════

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_data = context.bot_data
    monitoring = bot_data.get("monitoring", False)
    uptime = bot_data.get("uptime_start")
    alerts_sent = bot_data.get("alerts_sent", 0)
    symbols_count = bot_data.get("symbols_count", 0)
    last_cycle = bot_data.get("last_cycle", "—")

    uptime_str = "—"
    if uptime:
        delta = datetime.now(TZ_IT) - uptime
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m = rem // 60
        uptime_str = f"{h}h {m}m"

    has_creds = _has_credentials()
    active_alerts = al.get_all_states()
    alert_list = "\n".join(
        f"  • {sym} ({d['level'].upper()})" for sym, d in active_alerts.items()
    ) or "  Nessuno"

    key = os.getenv("BYBIT_API_KEY", "")
    secret = os.getenv("BYBIT_API_SECRET", "")
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("CHAT_ID", "—")

    text = (
        "🤖 *FUNDING KING BOT — Status*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🔌 *Connessioni*\n"
        f"  Telegram: {'✅' if token else '❌'}\n"
        f"  Bybit API: {'✅' if has_creds else '❌ Credenziali mancanti'}\n\n"
        "🔑 *Credenziali*\n"
        f"  Token: `{_mask(token)}`\n"
        f"  Chat ID: `{chat_id}`\n"
        f"  API Key: `{_mask(key) if key else '⚠️ non impostata'}`\n"
        f"  API Secret: `{_mask(secret) if secret else '⚠️ non impostato'}`\n\n"
        "⚙️ *Bot*\n"
        f"  Stato: {'✅ Attivo' if monitoring else '⏸ In attesa'}\n"
        f"  Simboli monitorati: {symbols_count}\n"
        f"  Uptime: {uptime_str}\n"
        f"  Alert inviati: {alerts_sent}\n\n"
        "🕐 *Ultimo ciclo*\n"
        f"  {last_cycle}\n\n"
        "📊 *Simboli in alert ora*\n"
        f"{alert_list}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# /test
# ══════════════════════════════════════════════════════════════════════════════

async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Avvio test connessione Bybit...")
    results = await bc.test_connection()

    p = results.get("public", {})
    a = results.get("auth", {})
    pos = results.get("positions", {})

    total_ms = sum(r.get("latency_ms", 0) for r in results.values() if r.get("latency_ms", 0) > 0)

    pub_line = (
        f"✅ OK — {p['latency_ms']} ms — {p.get('symbols', '?')} simboli"
        if p.get("ok") else
        f"❌ FAIL — {p.get('error', '?')} ({p.get('latency_ms', '?')} ms)"
    )
    auth_line = (
        f"✅ OK — {a['latency_ms']} ms — Equity: ${a.get('equity', 0):,.2f}"
        if a.get("ok") else
        f"❌ FAIL — {a.get('error', '?')} ({a.get('latency_ms', '?')} ms)"
    )
    pos_line = (
        f"✅ OK — {pos['latency_ms']} ms — {pos.get('open', 0)} posizioni aperte"
        if pos.get("ok") else
        f"❌ FAIL — {pos.get('error', '?')}"
    )
    # Aggiungi dettaglio per-categoria se ci sono errori
    detail = pos.get("detail", {})
    detail_lines = []
    for lbl, d in detail.items():
        if isinstance(d, dict):
            code = d.get("retCode", "?")
            msg  = d.get("retMsg", d.get("error", ""))
            nz   = d.get("nonzero", 0)
            icon = "✅" if code == 0 else "⚠️"
            detail_lines.append(f"   {icon} [{lbl}] code={code} pos={nz} {msg[:40] if msg else ''}")
    pos_detail_str = "\n" + "\n".join(detail_lines) if detail_lines else ""

    all_ok = p.get("ok") and a.get("ok") and pos.get("ok")
    summary = "✅ Tutti i test superati" if all_ok else "⚠️ Alcuni test falliti"

    text = (
        f"🔧 *TEST CONNESSIONE BYBIT*\n\n"
        f"1️⃣ API Pubblica\n   {pub_line}\n\n"
        f"2️⃣ API Autenticata\n   {auth_line}\n\n"
        f"3️⃣ Posizioni\n   {pos_line}{pos_detail_str}\n\n"
        f"⏱ Tempo totale: {total_ms} ms\n"
        f"{summary}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# /funding_top & /funding_bottom
# ══════════════════════════════════════════════════════════════════════════════

async def funding_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Recupero funding positivi...")
    tickers = await bc.get_funding_tickers()
    tickers_sorted = sorted(
        tickers,
        key=lambda t: float(t.get("fundingRate", 0)),
        reverse=True,
    )[:10]

    if not tickers_sorted:
        await update.message.reply_text("Nessun dato disponibile.")
        return

    lines = ["📈 *TOP 10 FUNDING POSITIVI (SHORT)*\n"]
    for i, t in enumerate(tickers_sorted, 1):
        rate = float(t.get("fundingRate", 0)) * 100
        interval = t.get("fundingIntervalHour", "?")
        lines.append(f"{i}. `{t['symbol']}` → *{rate:+.4f}%* ogni {interval}H")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def funding_bottom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Recupero funding negativi...")
    tickers = await bc.get_funding_tickers()
    tickers_sorted = sorted(
        tickers,
        key=lambda t: float(t.get("fundingRate", 0)),
    )[:10]

    if not tickers_sorted:
        await update.message.reply_text("Nessun dato disponibile.")
        return

    lines = ["📉 *TOP 10 FUNDING NEGATIVI (LONG)*\n"]
    for i, t in enumerate(tickers_sorted, 1):
        rate = float(t.get("fundingRate", 0)) * 100
        interval = t.get("fundingIntervalHour", "?")
        lines.append(f"{i}. `{t['symbol']}` → *{rate:+.4f}%* ogni {interval}H")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# /storico <SIMBOLO>
# ══════════════════════════════════════════════════════════════════════════════

async def storico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /storico BTCUSDT      — ultimi 8 cicli
    /storico BTCUSDT 7g   — storico 7 giorni con statistiche
    """
    args = context.args
    if not args:
        await update.message.reply_text(
            "📅 *Uso:*\n"
            "`/storico BTCUSDT`    — ultimi 8 cicli\n"
            "`/storico BTCUSDT 7g` — storico 7 giorni con statistiche",
            parse_mode="Markdown"
        )
        return

    symbol = args[0].upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    mode_7g = len(args) >= 2 and args[1].lower() in ("7g", "7d", "7", "week")

    if mode_7g:
        await storico7g_impl(update, symbol)
    else:
        await update.message.reply_text(f"📊 Storico funding {symbol}...")
        history = await bc.get_funding_history(symbol, limit=8)
        if not history:
            await update.message.reply_text(
                f"❌ Nessun dato per `{symbol}`.\nControlla che il simbolo sia corretto.",
                parse_mode="Markdown"
            )
            return
        lines = [f"📅 *STORICO FUNDING — {symbol}*\n━━━━━━━━━━━━━━━━━━"]
        for entry in history:
            rate = float(entry.get("fundingRate", 0)) * 100
            ts = int(entry.get("fundingRateTimestamp", 0)) // 1000
            dt = datetime.fromtimestamp(ts, tz=TZ_IT).strftime("%d/%m %H:%M")
            emoji = "🟢" if rate >= 0 else "🔴"
            lines.append(f"{emoji} `{dt}` → `{rate:+.4f}%`")
        lines.append("\n_/storico " + symbol + " 7g_ per storico completo")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# /storico7g <SIMBOLO> — Storico 7 giorni con mini-chart e statistiche
# ══════════════════════════════════════════════════════════════════════════════

# Blocchi unicode per il mini-chart (8 livelli: da quasi zero a massimo)
_BARS = " ▁▂▃▄▅▆▇█"


def _spark(values: list[float]) -> str:
    """Genera una sparkline unicode da una lista di valori assoluti."""
    if not values:
        return ""
    vmin = min(values)
    vmax = max(values)
    span = vmax - vmin or 1e-9
    chars = []
    for v in values:
        idx = int((v - vmin) / span * (len(_BARS) - 1))
        chars.append(_BARS[idx])
    return "".join(chars)


def _trend_emoji(rates: list[float]) -> str:
    """Freccia di tendenza basata sul confronto prima metà vs seconda metà."""
    if len(rates) < 4:
        return "➡️"
    mid   = len(rates) // 2
    first = sum(abs(r) for r in rates[:mid]) / mid
    last  = sum(abs(r) for r in rates[mid:]) / (len(rates) - mid)
    if last > first * 1.1:
        return "📈"
    if last < first * 0.9:
        return "📉"
    return "➡️"


async def storico7g_impl(update, symbol: str):
    """Storico 7 giorni — chiamato sia da /storico SYMBOL 7g che come funzione interna."""
    await update.message.reply_text(f"📅 Recupero storico 7 giorni — *{symbol}*...", parse_mode="Markdown")

    history = await bc.get_funding_history_7d(symbol)
    if not history:
        await update.message.reply_text(
            f"❌ Nessun dato trovato per `{symbol}`.\n"
            "Controlla che il simbolo sia corretto (es. `BTCUSDT`).",
            parse_mode="Markdown",
        )
        return

    # Ordina dal meno recente al più recente per il chart
    entries = sorted(history, key=lambda e: int(e.get("fundingRateTimestamp", 0)))
    rates   = [float(e.get("fundingRate", 0)) * 100 for e in entries]
    abs_rates = [abs(r) for r in rates]
    timestamps = [int(e.get("fundingRateTimestamp", 0)) // 1000 for e in entries]

    # ── Statistiche globali ───────────────────────────────────────────────────
    avg_rate  = sum(rates) / len(rates)
    avg_abs   = sum(abs_rates) / len(abs_rates)
    max_rate  = max(rates)
    min_rate  = min(rates)
    max_idx   = rates.index(max_rate)
    min_idx   = rates.index(min_rate)
    max_dt    = datetime.fromtimestamp(timestamps[max_idx], tz=TZ_IT).strftime("%d/%m %H:%M")
    min_dt    = datetime.fromtimestamp(timestamps[min_idx], tz=TZ_IT).strftime("%d/%m %H:%M")
    last_rate = rates[-1]  # più recente
    trend     = _trend_emoji(rates)

    # Conta cicli positivi vs negativi
    pos_count = sum(1 for r in rates if r > 0)
    neg_count = sum(1 for r in rates if r < 0)
    neu_count = len(rates) - pos_count - neg_count

    # ── Mini-chart (max 40 caratteri) ─────────────────────────────────────────
    # Raggruppa se ci sono troppi punti
    chart_values = abs_rates
    if len(chart_values) > 40:
        # Sottocampiona a 40 punti
        step = len(chart_values) / 40
        chart_values = [chart_values[int(i * step)] for i in range(40)]
    spark = _spark(chart_values)

    # ── Media per giorno ──────────────────────────────────────────────────────
    from collections import defaultdict
    daily: dict[str, list[float]] = defaultdict(list)
    for rate, ts in zip(rates, timestamps):
        day = datetime.fromtimestamp(ts, tz=TZ_IT).strftime("%d/%m")
        daily[day].append(rate)

    daily_lines = []
    for day in sorted(daily.keys(), key=lambda d: datetime.strptime(d, "%d/%m").replace(year=2026)):
        day_rates = daily[day]
        day_avg   = sum(day_rates) / len(day_rates)
        day_max   = max(day_rates)
        day_min   = min(day_rates)
        emoji = "🟢" if day_avg > 0.01 else ("🔴" if day_avg < -0.01 else "⚪")
        # Barra visiva proporzionale (max 10 caratteri)
        bar_len = min(10, max(1, int(abs(day_avg) / max(avg_abs, 0.001) * 10)))
        bar = ("█" * bar_len).ljust(10)
        daily_lines.append(
            f"  {day}  {emoji} `{day_avg:+.4f}%`  |{bar}|  ({len(day_rates)} cicli)"
        )

    # ── Intervallo del simbolo ────────────────────────────────────────────────
    # Calcola intervallo medio dai timestamp
    if len(timestamps) >= 2:
        diffs = [(timestamps[i+1] - timestamps[i]) / 3600 for i in range(len(timestamps)-1)]
        avg_interval = sum(diffs) / len(diffs)
        if avg_interval <= 1.1:
            interval_str = "1H"
        elif avg_interval <= 2.1:
            interval_str = "2H"
        elif avg_interval <= 4.1:
            interval_str = "4H"
        else:
            interval_str = "8H"
    else:
        interval_str = "?"

    # ── Composizione messaggio ────────────────────────────────────────────────
    lines = [
        f"📅 *STORICO 7 GIORNI — {symbol}* {trend}",
        "",
        f"*Andamento funding (valore assoluto):*",
        f"`{spark}`",
        f"  ↑ max    ↓ min",
        "",
        "📊 *Statistiche globali:*",
        f"  Media (signed):  `{avg_rate:+.4f}%`",
        f"  Media (assoluta):`{avg_abs:+.4f}%`",
        f"  Max:  `{max_rate:+.4f}%`  ({max_dt})",
        f"  Min:  `{min_rate:+.4f}%`  ({min_dt})",
        f"  Attuale (ultimo): `{last_rate:+.4f}%`",
        "",
        f"  🟢 Positivi: {pos_count}  🔴 Negativi: {neg_count}  ⚪ Neutri: {neu_count}",
        "",
        "📆 *Media giornaliera:*",
    ] + daily_lines + [
        "",
        f"⏱ Intervallo: {interval_str}  |  Cicli analizzati: {len(rates)}",
    ]

    # Telegram ha limite 4096 caratteri per messaggio
    msg = "\n".join(lines)
    if len(msg) > 4000:
        # Invia in due parti
        split = lines.index("📆 *Media giornaliera:*")
        await update.message.reply_text("\n".join(lines[:split]), parse_mode="Markdown")
        await update.message.reply_text("\n".join(lines[split:]), parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# /saldo
# ══════════════════════════════════════════════════════════════════════════════

async def saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id   = update.effective_chat.id
    exchanges = _user_exchanges(chat_id)

    # Fallback legacy
    if not exchanges:
        if not _has_credentials():
            await update.message.reply_text("⚠️ Configure your credentials with /start.")
            return
        await update.message.reply_text("💼 Fetching balance...")
        wallet = await bc.get_wallet_balance()
        if not wallet:
            await update.message.reply_text("❌ Cannot fetch balance. Check API keys with /test.")
            return
        pnl_emoji = "✅" if wallet["totalPerpUPL"] >= 0 else "❌"
        lines = [
            "💼 *BALANCE — Bybit*\n",
            f"Total equity:     `${wallet['totalEquity']:>12,.2f}`",
            f"Wallet balance:   `${wallet['totalWalletBalance']:>12,.2f}`",
            f"Available margin: `${wallet['totalAvailableBalance']:>12,.2f}`",
            f"Open PnL:         `${wallet['totalPerpUPL']:>+12,.2f}` {pnl_emoji}",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    await update.message.reply_text("💼 Fetching balances...")

    all_lines = []
    for ex in exchanges:
        client = _registry.get_client(chat_id, ex)
        if not client:
            continue
        emoji = EXCHANGE_EMOJI.get(ex, "🏦")
        name  = EXCHANGE_NAME.get(ex, ex.capitalize())
        try:
            wb = await client.get_wallet_balance()
            if not wb:
                all_lines.append(f"{emoji} *{name}* — ❌ No data")
                continue
            pnl_emoji = "✅" if wb.unrealized_pnl >= 0 else "❌"
            all_lines += [
                f"{emoji} *{name}*",
                f"  Equity:    `${wb.total_equity:>12,.2f}`",
                f"  Available: `${wb.available_balance:>12,.2f}`",
                f"  Open PnL:  `${wb.unrealized_pnl:>+12,.2f}` {pnl_emoji}",
                "",
            ]
        except Exception as e:
            all_lines.append(f"{emoji} *{name}* — ❌ Error: {e}")

    if not all_lines:
        await update.message.reply_text("❌ Cannot fetch balance for any exchange.")
        return

    header = f"💼 *BALANCE OVERVIEW*\n{'━'*20}\n"
    await update.message.reply_text(header + "\n".join(all_lines), parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# /posizioni
# ══════════════════════════════════════════════════════════════════════════════

async def posizioni(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id   = update.effective_chat.id
    exchanges = _user_exchanges(chat_id)

    if not exchanges:
        await update.message.reply_text("⚠️ Configure your credentials with /start.")
        return
    await update.message.reply_text("📋 Fetching positions...")

    all_lines = []
    total_pnl_all = 0.0

    for ex in exchanges:
        client = _registry.get_client(chat_id, ex)
        if not client:
            continue
        emoji = EXCHANGE_EMOJI.get(ex, "🏦")
        name  = EXCHANGE_NAME.get(ex, ex.capitalize())
        try:
            positions = await client.get_positions()
        except Exception as e:
            all_lines.append(f"{emoji} *{name}* — ❌ Error: {e}\n")
            continue

        if not positions:
            all_lines.append(f"{emoji} *{name}* — 📭 No open positions\n")
            continue

        all_lines.append(f"{emoji} *{name}*")
        total_pnl = 0.0
        for i, p in enumerate(positions, 1):
            side_emoji = "🟢" if p.side == "Buy" else "🔴"
            direction  = "LONG" if p.side == "Buy" else "SHORT"
            pnl        = p.unrealized_pnl
            total_pnl += pnl
            total_pnl_all += pnl
            pnl_emoji  = "✅" if pnl >= 0 else "❌"
            entry      = p.entry_price
            mark       = p.mark_price
            liq        = p.liq_price
            block = [
                f"  {i}) *{p.symbol}* {side_emoji} {direction} x{p.leverage}",
                f"     Size: `{p.size}`  Entry: `{entry:,.4f}`",
                f"     Mark: `{mark:,.4f}`  Liq: `{liq:,.4f}`",
                f"     PnL:  `{pnl:+,.2f} USDT` {pnl_emoji}",
                "",
            ]
            all_lines.extend(block)
        sign = "+" if total_pnl >= 0 else ""
        all_lines.append(f"  ∑ PnL {name}: `{sign}{total_pnl:.2f} USDT`")
        all_lines.append("")

    if not any(l.strip() for l in all_lines):
        await update.message.reply_text("📭 *No open positions on any exchange.*", parse_mode="Markdown")
        return

    sign_all = "+" if total_pnl_all >= 0 else ""
    footer = f"{'━'*20}\n🔢 Total open PnL: `{sign_all}{total_pnl_all:.2f} USDT`"
    header = f"📋 *OPEN POSITIONS*\n{'━'*20}\n"
    await update.message.reply_text(header + "\n".join(all_lines) + "\n" + footer, parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# Watchlist persistente: /watch /unwatch /mute /unmute /watchlist /alerts
# ══════════════════════════════════════════════════════════════════════════════

# Cache simboli validi Bybit (aggiornata al primo uso)
_known_symbols: set[str] = set()


async def _get_known_symbols() -> set[str]:
    global _known_symbols
    if not _known_symbols:
        tickers = await bc.get_funding_tickers()
        _known_symbols = {t["symbol"] for t in tickers}
    return _known_symbols


async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        wl = wm.get_watchlist()
        mode = "filtro attivo" if wl else "tutti i simboli"
        await update.message.reply_text(
            f"*Uso:* `/watch BTC ETH SOL` \n_(aggiunge USDT automaticamente)_\n\n"
            f"Watchlist attuale: *{mode}*",
            parse_mode="Markdown",
        )
        return

    known = await _get_known_symbols()
    raw   = context.args
    valid, unknown = wm.validate_symbols(raw, known)

    if valid:
        added = wm.add_symbols(valid)
        wl    = wm.get_watchlist()
        lines = [f"✅ *Watchlist aggiornata* ({len(wl)} simboli)\n"]
        for s in sorted(wl):
            alert_state = al._state.get(s, {}).get("level", "none")
            badge = " 🔴" if alert_state != "none" else ""
            custom = wm.get_all_custom_thresholds().get(s)
            custom_tag = " ⚙️" if custom else ""
            muted = "🔇" if s in wm.get_muted() else ""
            lines.append(f"  • `{s}`{badge}{custom_tag}{muted}")
    else:
        lines = []

    if unknown:
        lines.append(f"\n⚠️ Non trovati su Bybit: `{'`, `'.join(unknown)}`")

    if not valid and not unknown:
        lines = ["⚠️ Nessun simbolo valido specificato."]

    lines.append("\n_⚙️ = soglie custom  🔴 = in alert  🔇 = silenziato_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def unwatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "*Uso:* `/unwatch BTCUSDT ETHUSDT`\n"
            "Per rimuovere tutti: `/unwatch all`",
            parse_mode="Markdown",
        )
        return

    if context.args[0].lower() == "all":
        wm.clear_watchlist()
        await update.message.reply_text(
            "✅ Watchlist svuotata. Il bot monitora ora *tutti* i simboli.",
            parse_mode="Markdown",
        )
        return

    raw     = context.args
    symbols = [s.upper() if s.upper().endswith("USDT") else s.upper() + "USDT" for s in raw]
    removed = wm.remove_symbols(symbols)
    not_found = [s for s in symbols if s not in removed]

    lines = []
    if removed:
        lines.append(f"✅ Rimossi: `{'`, `'.join(removed)}`")
    if not_found:
        lines.append(f"⚠️ Non erano in watchlist: `{'`, `'.join(not_found)}`")

    wl = wm.get_watchlist()
    if wl:
        lines.append(f"\nWatchlist: {', '.join(f'`{s}`' for s in sorted(wl))}")
    else:
        lines.append("\nWatchlist vuota — monitor *tutti* i simboli.")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        muted = wm.get_muted()
        msg = (
            f"🔇 *Simboli silenziati:* {', '.join(f'`{s}`' for s in sorted(muted))}"
            if muted else
            "🔇 *Nessun simbolo silenziato.*\n*Uso:* `/mute BTCUSDT`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    known   = await _get_known_symbols()
    valid, unknown = wm.validate_symbols(context.args, known)
    added   = wm.mute_symbols(valid)

    lines = []
    if added:
        lines.append(f"🔇 Silenziati: `{'`, `'.join(added)}`")
    if unknown:
        lines.append(f"⚠️ Non trovati: `{'`, `'.join(unknown)}`")
    await update.message.reply_text("\n".join(lines) or "Nessun simbolo modificato.", parse_mode="Markdown")


async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("*Uso:* `/unmute BTCUSDT`", parse_mode="Markdown")
        return

    symbols = [s.upper() if s.upper().endswith("USDT") else s.upper() + "USDT" for s in context.args]
    removed = wm.unmute_symbols(symbols)
    lines   = []
    if removed:
        lines.append(f"🔔 Riattivati: `{'`, `'.join(removed)}`")
    else:
        lines.append("⚠️ Nessuno di questi simboli era silenziato.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def watchlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    summary = wm.get_summary()
    wl      = summary["watchlist"]
    muted   = summary["muted"]
    custom  = summary["custom_thresholds"]
    mode    = summary["mode"]

    # Sezione watchlist
    if wl:
        wl_lines = []
        for s in sorted(wl):
            alert_state = al._state.get(s, {}).get("level", "none")
            badges = []
            if s in muted:                    badges.append("🔇")
            if alert_state != "none":         badges.append(f"🔴{alert_state.upper()}")
            if s in custom:                   badges.append("⚙️")
            badge_str = "  " + " ".join(badges) if badges else ""
            wl_lines.append(f"  • `{s}`{badge_str}")
        wl_section = "\n".join(wl_lines)
    else:
        wl_section = "  _(tutti i simboli Bybit — nessun filtro)_"

    # Sezione muted
    muted_section = (
        "  " + ", ".join(f"`{s}`" for s in sorted(muted))
        if muted else
        "  _(nessuno)_"
    )

    # Sezione soglie custom
    if custom:
        custom_lines = []
        for sym, levels in sorted(custom.items()):
            parts = [f"{lvl}: {val}%" for lvl, val in sorted(levels.items())]
            custom_lines.append(f"  `{sym}` — {', '.join(parts)}")
        custom_section = "\n".join(custom_lines)
    else:
        custom_section = "  _(usa soglie globali per tutti)_"

    text = (
        f"🎯 *WATCHLIST — Modalità: {mode}*\n\n"
        f"📡 *Simboli monitorati:*\n{wl_section}\n\n"
        f"🔇 *Silenziati:*\n{muted_section}\n\n"
        f"⚙️ *Soglie custom:*\n{custom_section}\n\n"
        f"_Usa /watch, /unwatch, /mute, /unmute, /alerts_"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# /alerts — Gestione soglie custom per simbolo
# ══════════════════════════════════════════════════════════════════════════════

_LEVEL_NAMES = {
    "hard": "HARD (default 2.00%)",
    "extreme": "EXTREME (default 1.50%)",
    "high": "HIGH (default 1.00%)",
    "close_tip": "CHIUSURA (default 0.23%)",
    "rientro": "RIENTRO (default 0.75%)",
}


async def alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Uso:
      /alerts                          — mostra tutte le soglie custom
      /alerts BTCUSDT                  — mostra soglie per il simbolo
      /alerts BTCUSDT high 1.5         — imposta HIGH a 1.5% per BTC
      /alerts BTCUSDT reset            — riporta BTC ai default globali
    """
    args = context.args

    # Nessun argomento: mostra riepilogo globale
    if not args:
        custom = wm.get_all_custom_thresholds()
        if not custom:
            await update.message.reply_text(
                "ℹ️ *No custom thresholds set.*\n\n"
                "Tutti i simboli usano le soglie globali:\n"
                "  🔴 HARD: 2.00%\n"
                "  🔥 EXTREME: 1.50%\n"
                "  🚨 HIGH: 1.00%\n"
                "  ℹ️ CHIUSURA: 0.23%\n"
                "  ✅ RIENTRO: 0.75%\n\n"
                "*Uso:* `/alerts BTCUSDT high 1.5`\n"
                "*Reset:* `/alerts BTCUSDT reset`",
                parse_mode="Markdown",
            )
            return

        lines = ["⚙️ *SOGLIE CUSTOM ATTIVE*\n"]
        for sym, levels in sorted(custom.items()):
            lines.append(f"*{sym}*")
            for lvl, val in sorted(levels.items()):
                default = {"hard": 2.0, "extreme": 1.5, "high": 1.0, "close_tip": 0.23, "rientro": 0.75}.get(lvl, 0)
                arrow = "↑" if val > default else "↓"
                lines.append(f"  {lvl}: `{val}%` {arrow} _(default: {default}%)_")
        lines.append("\n_/alerts SIMBOLO reset per tornare ai default_")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    symbol = args[0].upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    # /alerts BTCUSDT reset
    if len(args) == 2 and args[1].lower() == "reset":
        wm.remove_custom_thresholds(symbol)
        await update.message.reply_text(
            f"✅ Soglie di `{symbol}` ripristinate ai valori globali.",
            parse_mode="Markdown",
        )
        return

    # /alerts BTCUSDT  (mostra soglie del simbolo)
    if len(args) == 1:
        custom = wm.get_all_custom_thresholds().get(symbol, {})
        defaults = {"hard": 2.0, "extreme": 1.5, "high": 1.0, "close_tip": 0.23, "rientro": 0.75}
        lines = [f"⚙️ *Soglie per {symbol}*\n"]
        for lvl, default in defaults.items():
            val = custom.get(lvl, default)
            tag = " _⚙️ custom_" if lvl in custom else " _default_"
            lines.append(f"  {lvl}: `{val}%`{tag}")
        lines.append(f"\n*Uso:* `/alerts {symbol} high 1.5`")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    # /alerts BTCUSDT high 1.5
    if len(args) == 3:
        level = args[1].lower()
        try:
            value = float(args[2].replace(",", "."))
        except ValueError:
            await update.message.reply_text(
                f"❌ Valore non valido: `{args[2]}`\nUsa un numero (es. 1.5)",
                parse_mode="Markdown",
            )
            return

        if value <= 0 or value > 10:
            await update.message.reply_text(
                "❌ Il valore deve essere tra 0 e 10.",
                parse_mode="Markdown",
            )
            return

        ok = wm.set_custom_threshold(symbol, level, value)
        if not ok:
            levels_str = ", ".join(f"`{l}`" for l in _LEVEL_NAMES)
            await update.message.reply_text(
                f"❌ Livello `{level}` non valido.\nLivelli disponibili: {levels_str}",
                parse_mode="Markdown",
            )
            return

        default = {"hard": 2.0, "extreme": 1.5, "high": 1.0, "close_tip": 0.23, "rientro": 0.75}.get(level, 0)
        arrow = "↑ più restrittivo" if value > default else "↓ più sensibile"
        await update.message.reply_text(
            f"✅ Soglia custom impostata:\n"
            f"  `{symbol}` — {level}: `{value}%` ({arrow}, default: {default}%)",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        "*Uso:*\n"
        "`/alerts` — riepilogo globale\n"
        "`/alerts BTCUSDT` — soglie del simbolo\n"
        "`/alerts BTCUSDT high 1.5` — imposta soglia\n"
        "`/alerts BTCUSDT reset` — ripristina default",
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════════════════════════════════════════
# Helper privati
# ══════════════════════════════════════════════════════════════════════════════

def _mask(value: str) -> str:
    if not value or len(value) < 8:
        return "****"
    return value[:4] + "****" + value[-4:]


def _set_env(key: str, value: str):
    """Imposta una variabile d'ambiente in memoria e nel file .env."""
    os.environ[key] = value
    env_path = ".env"
    lines = []
    updated = False
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={value}\n"
                updated = True
                break
    if not updated:
        lines.append(f"{key}={value}\n")
    with open(env_path, "w") as f:
        f.writelines(lines)


# ══════════════════════════════════════════════════════════════════════════════
# /top10 — Classifica unificata in tempo reale
# ══════════════════════════════════════════════════════════════════════════════

# Numero di simboli per lato (SHORT / LONG)
_TOP_N = 10

# Barra proporzionale (max 12 █)
_BAR_MAX = 12


def _rate_bar(rate_pct: float, max_abs: float) -> str:
    """Genera una barra █ proporzionale al rate rispetto al massimo della lista."""
    if max_abs == 0:
        return "▏"
    length = max(1, int(abs(rate_pct) / max_abs * _BAR_MAX))
    return "█" * length


def _level_badge(abs_rate: float) -> str:
    """Restituisce il badge di livello in base alle soglie fisse."""
    if abs_rate >= 2.00:
        return "🔴HARD"
    if abs_rate >= 1.50:
        return "🔥EXT"
    if abs_rate >= 1.00:
        return "🚨HIGH"
    if abs_rate >= 0.23:
        return "ℹ️CHI"
    return "✅OK"


def _settlement_label(next_ts_ms: int) -> str:
    """Restituisce il tempo mancante al prossimo settlement in formato leggibile."""
    if not next_ts_ms:
        return "—"
    import time
    minutes_left = (next_ts_ms - int(time.time() * 1000)) / 60000
    if minutes_left < 0:
        return "ora"
    if minutes_left < 60:
        return f"{int(minutes_left)}m"
    h = int(minutes_left // 60)
    m = int(minutes_left % 60)
    return f"{h}h{m:02d}m"


async def top10(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /top10 — Classifica dei 10 simboli con funding rate più estremi
    per lato SHORT (positivi) e LONG (negativi), in tempo reale.
    """
    msg = await update.message.reply_text("⏳ Recupero dati in tempo reale...")

    tickers = await bc.get_funding_tickers()
    if not tickers:
        await msg.edit_text("❌ Impossibile recuperare i dati da Bybit. Riprova.")
        return

    # Parsing e ordinamento
    parsed = []
    for t in tickers:
        try:
            rate_pct     = float(t.get("fundingRate", 0)) * 100
            interval_h   = int(t.get("fundingIntervalHour", 8))
            next_ts      = int(t.get("nextFundingTime", 0))
            last_price   = float(t.get("lastPrice", 0))
            pct_24h      = float(t.get("price24hPcnt", 0)) * 100
            parsed.append({
                "symbol":     t["symbol"],
                "rate":       rate_pct,
                "interval_h": interval_h,
                "next_ts":    next_ts,
                "last_price": last_price,
                "pct_24h":    pct_24h,
            })
        except (ValueError, KeyError):
            continue

    # Top 10 SHORT (rate più positivi)
    shorts = sorted(parsed, key=lambda x: x["rate"], reverse=True)[:_TOP_N]
    # Top 10 LONG  (rate più negativi)
    longs  = sorted(parsed, key=lambda x: x["rate"])[:_TOP_N]

    max_short = abs(shorts[0]["rate"]) if shorts else 1
    max_long  = abs(longs[0]["rate"])  if longs  else 1

    now_dt = datetime.now(TZ_IT).strftime("%H:%M %Z")

    # ── Sezione SHORT ─────────────────────────────────────────────────────────
    short_lines = [
        f"⚡ *TOP {_TOP_N} SHORT* (funding positivo)",
        f"_Aggiornato: {now_dt}_",
        "`#   Simbolo        Rate      Lvl   Next  24H`",
        "`─────────────────────────────────────────────`",
    ]
    for i, t in enumerate(shorts, 1):
        bar      = _rate_bar(t["rate"], max_short)
        badge    = _level_badge(t["rate"])
        settle   = _settlement_label(t["next_ts"])
        p24h     = f"{t['pct_24h']:+.1f}%"
        interval = f"{t['interval_h']}H"
        sym_esc = t['symbol'].replace('_', r'\_')
        short_lines.append(
            f"`{i:>2}.` *{sym_esc}* `{t['rate']:+.4f}%`\n"
            f"     `{bar:<12}` {badge} · {interval} · {settle} · {p24h}"
        )

    # ── Sezione LONG ──────────────────────────────────────────────────────────
    long_lines = [
        "",
        f"⚡ *TOP {_TOP_N} LONG* (funding negativo)",
        "`#   Simbolo        Rate      Lvl   Next  24H`",
        "`─────────────────────────────────────────────`",
    ]
    for i, t in enumerate(longs, 1):
        bar      = _rate_bar(t["rate"], max_long)
        badge    = _level_badge(abs(t["rate"]))
        settle   = _settlement_label(t["next_ts"])
        p24h     = f"{t['pct_24h']:+.1f}%"
        interval = f"{t['interval_h']}H"
        sym_esc = t['symbol'].replace('_', r'\_')
        long_lines.append(
            f"`{i:>2}.` *{sym_esc}* `{t['rate']:+.4f}%`\n"
            f"     `{bar:<12}` {badge} · {interval} · {settle} · {p24h}"
        )

    # ── Footer statistiche ────────────────────────────────────────────────────
    total_sym   = len(parsed)
    extreme_sym = sum(1 for t in parsed if abs(t["rate"]) >= 1.0)
    hard_sym    = sum(1 for t in parsed if abs(t["rate"]) >= 2.0)
    avg_abs     = sum(abs(t["rate"]) for t in parsed) / total_sym if total_sym else 0

    footer = [
        "",
        "─────────────────────────────",
        f"📊 *Mercato* — {total_sym} simboli monitorati",
        f"   🚨 ≥1%: {extreme_sym}   🔴 ≥2%: {hard_sym}   Media: {avg_abs:.4f}%",
    ]

    # ── Invio ────────────────────────────────────────────────────────────────
    full_msg = "\n".join(short_lines + long_lines + footer)

    # Telegram: max 4096 char — se supera split in 2
    if len(full_msg) > 4000:
        part1 = "\n".join(short_lines + footer)
        part2 = "\n".join(long_lines[1:] + footer)  # [1:] salta riga vuota iniziale
        await msg.edit_text(part1, parse_mode="Markdown")
        await update.message.reply_text(part2, parse_mode="Markdown")
    else:
        await msg.edit_text(full_msg, parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# BACKTEST
# ══════════════════════════════════════════════════════════════════════════════

async def backtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_plan(update, "pro"):
        return
    """
    /backtest <SYMBOL>         — Report completo su un simbolo (30gg)
    /backtest top10            — Classifica top 10 simboli più volatili
    /backtest watchlist        — Analizza tutti i simboli nella watchlist

    Esempi:
      /backtest SOLUSDT
      /backtest top10
      /backtest watchlist
    """
    args = context.args or []

    if not args:
        await update.message.reply_text(
            "📊 *BACKTEST — Uso corretto:*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "`/backtest SOLUSDT`       — Singolo simbolo\n"
            "`/backtest top10`         — Top 10 più volatili\n"
            "`/backtest watchlist`     — Tua watchlist\n\n"
            "_Simula profitti/perdite basati sugli alert del bot negli ultimi 30 giorni._\n"
            "_Include fee taker (0.055%) + slippage (0.02%) per lato._",
            parse_mode="Markdown",
        )
        return

    subcmd = args[0].upper()

    # ── /backtest top10 ───────────────────────────────────────────────────
    if subcmd == "TOP10":
        wait_msg = await update.message.reply_text(
            "⏳ *Backtest top 10 simboli…*\n"
            "_Recupero dati 30gg da Bybit (può richiedere 30-60 secondi)_",
            parse_mode="Markdown",
        )
        try:
            # Prendi i 10 simboli con funding rate assoluto più alto
            tickers = await bc.get_funding_tickers()
            if not tickers:
                await wait_msg.edit_text("❌ Impossibile recuperare i ticker da Bybit.")
                return

            top_symbols = sorted(
                tickers,
                key=lambda t: abs(float(t.get("fundingRate", 0))),
                reverse=True,
            )[:10]
            symbols = [t["symbol"] for t in top_symbols]

            results = await bt.run_multi_backtest(symbols)
            report  = bt.format_multi_backtest_report(results, title="TOP 10 SIMBOLI")

            await wait_msg.delete()
            # Splitta se troppo lungo
            if len(report) <= 4096:
                await update.message.reply_text(report, parse_mode="Markdown")
            else:
                for chunk in [report[i:i+4096] for i in range(0, len(report), 4096)]:
                    await update.message.reply_text(chunk, parse_mode="Markdown")

        except Exception as exc:
            logger.error("backtest top10: %s", exc)
            await wait_msg.edit_text(f"❌ Errore durante il backtest: {exc}")
        return

    # ── /backtest watchlist ───────────────────────────────────────────────
    if subcmd == "WATCHLIST":
        symbols = list(wm.get_watchlist())
        if not symbols:
            await update.message.reply_text(
                "⚠️ La tua watchlist è vuota.\n"
                "Aggiungi simboli con `/watch BTCUSDT SOLUSDT`",
                parse_mode="Markdown",
            )
            return

        wait_msg = await update.message.reply_text(
            f"⏳ *Backtest watchlist ({len(symbols)} simboli)…*\n"
            f"_Recupero dati 30gg da Bybit…_",
            parse_mode="Markdown",
        )
        try:
            results = await bt.run_multi_backtest(symbols)
            report  = bt.format_multi_backtest_report(results, title="WATCHLIST")

            await wait_msg.delete()
            if len(report) <= 4096:
                await update.message.reply_text(report, parse_mode="Markdown")
            else:
                for chunk in [report[i:i+4096] for i in range(0, len(report), 4096)]:
                    await update.message.reply_text(chunk, parse_mode="Markdown")

        except Exception as exc:
            logger.error("backtest watchlist: %s", exc)
            await wait_msg.edit_text(f"❌ Errore durante il backtest: {exc}")
        return

    # ── /backtest SYMBOL ──────────────────────────────────────────────────
    symbol = subcmd
    # Normalizza (aggiunge USDT se non presente)
    if not symbol.endswith("USDT") and not symbol.endswith("USDC"):
        symbol = symbol + "USDT"

    wait_msg = await update.message.reply_text(
        f"⏳ *Backtest {symbol}…*\n"
        f"_Recupero {bt.DAYS_BACK} giorni di funding rate da Bybit…_",
        parse_mode="Markdown",
    )

    try:
        entries = await bt.fetch_30d(symbol)
        if not entries:
            await wait_msg.edit_text(
                f"❌ Nessun dato trovato per `{symbol}`.\n"
                f"Verifica che il simbolo esista su Bybit.",
                parse_mode="Markdown",
            )
            return

        result = bt.run_backtest(symbol, entries)
        report = bt.format_backtest_report(result)

        await wait_msg.delete()
        if len(report) <= 4096:
            await update.message.reply_text(report, parse_mode="Markdown")
        else:
            for chunk in [report[i:i+4096] for i in range(0, len(report), 4096)]:
                await update.message.reply_text(chunk, parse_mode="Markdown")

    except Exception as exc:
        logger.error("backtest %s: %s", symbol, exc)
        await wait_msg.edit_text(f"❌ Errore durante il backtest di {symbol}: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# Registrazione handlers
# ══════════════════════════════════════════════════════════════════════════════


# ─────────────────────────────────────────────────────────────────────────────
# /profitto_funding
# ─────────────────────────────────────────────────────────────────────────────

async def profitto_funding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Mostra il riepilogo dei guadagni da funding per le posizioni aperte.

    Per ogni simbolo che ha ricevuto alert HIGH/EXTREME/HARD e aveva una
    posizione aperta, mostra:
      - Rate dell'ultimo ciclo di funding
      - Guadagno/costo dell'ultimo ciclo
      - Totale guadagno/costo da quando la posizione è aperta
    """
    if not _has_credentials(update.effective_chat.id):
        await update.message.reply_text("⚠️ Configura prima le tue API Key con /start")
        return

    await update.message.reply_text("💹 Recupero guadagni funding...")

    # Recupera posizioni aperte per arricchire il riepilogo
    try:
        positions = await bc.get_positions()
    except Exception:
        positions = []

    text = ft.format_summary(positions if positions else None)
    await update.message.reply_text(text, parse_mode="Markdown")




# ═══════════════════════════════════════════════════════════════════════════════
# /rischio — Analisi rischio posizioni aperte
# ═══════════════════════════════════════════════════════════════════════════════
async def rischio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Risk analysis for all open positions: liquidation distance, leverage, PnL%."""
    chat_id   = update.effective_chat.id
    exchanges = _user_exchanges(chat_id)
    if not exchanges:
        await update.message.reply_text("⚠️ Configure your credentials with /start.")
        return
    await update.message.reply_text("⚠️ Fetching risk analysis...")

    lines = ["⚠️ *RISK ANALYSIS*", ""]
    any_pos = False

    for ex in exchanges:
        client = _registry.get_client(chat_id, ex)
        if not client:
            continue
        emoji = EXCHANGE_EMOJI.get(ex, "🏦")
        name  = EXCHANGE_NAME.get(ex, ex.capitalize())
        try:
            positions = await client.get_positions()
        except Exception as e:
            lines.append(f"{emoji} *{name}* — ❌ Error: {e}\n")
            continue
        if not positions:
            continue

        lines.append(f"{emoji} *{name}*")
        for p in positions:
            any_pos    = True
            side_raw   = p.side
            side_lbl   = "🟢 LONG" if side_raw == "Buy" else "🔴 SHORT"
            mark       = p.mark_price
            liq        = p.liq_price
            lev        = p.leverage
            upnl       = p.unrealized_pnl
            size       = p.size

            if liq > 0 and mark > 0:
                dist_pct = (mark - liq) / mark * 100 if side_raw == "Buy" else (liq - mark) / mark * 100
                dist_pct = max(dist_pct, 0)
                risk_lbl = (
                    "🔴 CRITICAL" if dist_pct < 5 else
                    "🟠 HIGH"     if dist_pct < 10 else
                    "🟡 MEDIUM"   if dist_pct < 20 else
                    "🟢 LOW"
                )
                dist_str = f"{dist_pct:.1f}% ({risk_lbl})"
            else:
                dist_str = "N/A"

            sign = "+" if upnl >= 0 else ""
            lines += [
                f"  *{p.symbol}* {side_lbl} {lev}x",
                f"    Mark: `{mark:.4f}` | Liq: `{liq:.4f}`",
                f"    Liq distance: `{dist_str}`",
                f"    PnL: `{sign}{upnl:.2f} USDT`  Size: `{size}`",
                "",
            ]

    if not any_pos:
        await update.message.reply_text("📭 *No open positions on any exchange.*", parse_mode="Markdown")
        return
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
# /summary — Riepilogo rapido portafoglio
# ═══════════════════════════════════════════════════════════════════════════════
async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick portfolio summary: wallet + open positions across all exchanges."""
    chat_id   = update.effective_chat.id
    exchanges = _user_exchanges(chat_id)
    if not exchanges:
        await update.message.reply_text("⚠️ Configure your credentials with /start.")
        return
    await update.message.reply_text("📊 Building summary...")

    now_str = datetime.now(TZ_IT).strftime("%d/%m/%Y %H:%M")
    lines = [f"📊 *PORTFOLIO SUMMARY — {now_str}*", ""]
    total_equity = 0.0
    total_upnl   = 0.0
    total_pos    = 0

    for ex in exchanges:
        client = _registry.get_client(chat_id, ex)
        if not client:
            continue
        emoji = EXCHANGE_EMOJI.get(ex, "🏦")
        name  = EXCHANGE_NAME.get(ex, ex.capitalize())
        try:
            wb        = await client.get_wallet_balance()
            positions = await client.get_positions()
        except Exception as e:
            lines.append(f"{emoji} *{name}* — ❌ Error: {e}\n")
            continue

        eq     = wb.total_equity       if wb else 0.0
        avail  = wb.available_balance  if wb else 0.0
        upnl   = wb.unrealized_pnl     if wb else 0.0
        total_equity += eq
        total_upnl   += upnl
        total_pos    += len(positions)

        n_long  = sum(1 for p in positions if p.side == "Buy")
        n_short = sum(1 for p in positions if p.side == "Sell")
        pos_upnl = sum(p.unrealized_pnl for p in positions)

        lines += [
            f"{emoji} *{name}*",
            f"  Equity:    `{eq:,.2f} USDT`",
            f"  Available: `{avail:,.2f} USDT`",
            f"  Open PnL:  `{upnl:+,.2f} USDT`",
            f"  Positions: `{len(positions)}` (🟢 {n_long} LONG | 🔴 {n_short} SHORT)",
        ]
        if positions:
            best  = max(positions, key=lambda p: p.unrealized_pnl)
            worst = min(positions, key=lambda p: p.unrealized_pnl)
            lines.append(f"  🏆 Best:  {best.symbol} `{best.unrealized_pnl:+.2f}`")
            lines.append(f"  📉 Worst: {worst.symbol} `{worst.unrealized_pnl:+.2f}`")
        lines.append("")

    lines += [
        f"{'━'*20}",
        f"🔢 Total equity:   `{total_equity:,.2f} USDT`",
        f"📊 Total open PnL: `{total_upnl:+,.2f} USDT`",
        f"📂 Total positions:`{total_pos}`",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ═══════════════════════════════════════════════════════════════════════════════
# /newlistings — Nuovi listing con funding elevato
# ═══════════════════════════════════════════════════════════════════════════════
async def newlistings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra nuovi listing (ultimi 30gg) con funding rate elevato."""
    await update.message.reply_text("🆕 Recupero nuovi listing...")
    try:
        tickers = await bc.get_funding_tickers()
        # Ordina per funding rate assoluto decrescente e prendi i top 20
        items = sorted(tickers, key=lambda t: abs(float(t.get("fundingRate", 0))), reverse=True)[:20]
        items = [{"symbol": t["symbol"], "fundingRate": float(t.get("fundingRate",0))*100,
                  "markPrice": t.get("lastPrice", 0), "price24hPcnt": float(t.get("price24hPcnt",0))*100,
                  "daysAgo": 0} for t in items]
    except Exception as e:
        await update.message.reply_text(f"❌ Errore: {e}")
        return

    if not items:
        await update.message.reply_text("📭 Nessun nuovo listing trovato.")
        return

    # Filtra per funding rate notevole o mostra tutti
    notable = [i for i in items if abs(float(i.get("fundingRate", 0))) >= 0.5]
    show = notable if notable else items[:10]

    lines = [f"🆕 *NUOVI LISTING ({len(items)} totali, ultimi 30gg)*", ""]
    for item in show[:15]:
        sym  = item.get("symbol", "")
        fr   = float(item.get("fundingRate", 0))
        days = float(item.get("daysAgo", 0))
        mp   = float(item.get("markPrice", 0))
        pct  = float(item.get("price24hPcnt", 0))
        sign = "+" if fr >= 0 else ""
        fr_badge = "🔥" if abs(fr) >= 2.0 else "⚡" if abs(fr) >= 1.0 else "📊"
        lines.append(
            f"{fr_badge} *{sym}* — {days:.0f}gg fa\n"
            f"  FR: `{sign}{fr:.4f}%` | Price: `{mp:.4f}` ({pct:+.2f}%)"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
# /analytics — Metriche avanzate funding
# ═══════════════════════════════════════════════════════════════════════════════
async def analytics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra analytics avanzate: win-rate per livello, Sharpe, Sortino, drawdown."""
    await update.message.reply_text("📈 Calcolo analytics...")
    try:
        tickers = await bc.get_funding_tickers()
        data = {"ok": True, "total_records": len(tickers)}
    except Exception as e:
        await update.message.reply_text(f"❌ Errore fetch dati: {e}")
        return
    if not tickers:
        await update.message.reply_text("❌ Nessun dato disponibile")
        return

    total   = data.get("total_records", 0)
    t_gain  = data.get("total_gain", 0)
    wr      = data.get("win_rate", 0)
    sharpe  = data.get("sharpe", 0)
    sortino = data.get("sortino", 0)
    dd      = data.get("max_drawdown", 0)
    avg_g   = data.get("avg_gain", 0)
    by_lvl  = data.get("by_level", {})

    lines = [
        "📈 *ANALYTICS AVANZATI — FUNDING KING*", "",
        f"📊 Cicli registrati: `{total}`",
        f"💰 Gain totale: `{t_gain:+.4f} USDT`",
        f"✅ Win Rate globale: `{wr:.1f}%`",
        f"📉 Max Drawdown: `{dd:.4f} USDT`",
        f"📐 Sharpe Ratio: `{sharpe:.3f}`",
        f"📐 Sortino Ratio: `{sortino:.3f}`",
        f"📊 Avg Gain/ciclo: `{avg_g:+.4f} USDT`",
        "",
        "*Win Rate per livello:*",
    ]

    level_order = [("jackpot", "💎"), ("extreme", "🔥"), ("hard", "⚡"), ("high", "📊")]
    for lvl, emoji in level_order:
        d = by_lvl.get(lvl, {})
        if d.get("count", 0) > 0:
            lines.append(
                f"  {emoji} {lvl.upper()}: `{d.get('win_rate', 0):.1f}%` "
                f"({d.get('count', 0)} cicli | avg `{d.get('avg_gain', 0):+.4f}`)"
            )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
# /alert_config — Configura soglie alert
# ═══════════════════════════════════════════════════════════════════════════════
async def alert_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra e permette di configurare le soglie di alert."""
    import alert_logic as _al

    lines = [
        "⚙️ *CONFIGURAZIONE SOGLIE ALERT*", "",
        "*Soglie globali:*",
        f"  💎 JACKPOT:  `> {_al.THR_JACKPOT:.2f}%`",
        f"  🔥 EXTREME:  `> {_al.THR_EXTREME:.2f}%`",
        f"  ⚡ HARD:     `> {_al.THR_HARD:.2f}%`",
        f"  📊 HIGH:     `> {_al.THR_HIGH:.2f}%`",
        f"  ⬆️ CLOSE_TIP: `> {_al.THR_CLOSE_TIP:.2f}%`",
        f"  ⬇️ RIENTRO:  `< {_al.RESET_THRESHOLD:.2f}%`",
        "",
        "Per modificare le soglie usa i parametri nel file .env:",
        "`THR_JACKPOT`, `THR_EXTREME`, `THR_HARD`, `THR_HIGH`",
        "",
        "*Alert liquidazione imminente:*",
        "  🔴 Attivo quando distanza < 15% dal prezzo di liq.",
        "",
        "*Per aggiungere soglie custom per simbolo:*",
        "  `/alerts BTCUSDT` — mostra soglie correnti",
    ]

    try:
        lines.append("")
        lines.append("*Simboli con soglie custom:*")
        custom = _al.get_custom_thresholds() if hasattr(_al, 'get_custom_thresholds') else {}
        if custom:
            for sym, thr in list(custom.items())[:10]:
                lines.append(f"  • {sym}: `{thr:.2f}%`")
        else:
            lines.append("  Nessuna soglia custom impostata")
    except:
        pass

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")




# ══════════════════════════════════════════════════════════
# PARAMETRI DI RISCHIO — configurabili da /settings
# ══════════════════════════════════════════════════════════
_risk_params = {
    "max_leverage":       10.0,   # leva massima consentita per trade
    "max_positions":      10,     # numero massimo posizioni simultanee
    "max_pct_per_trade":  5.0,    # % massima del capitale per singolo trade
}

async def rischio_settings(update, context):
    if not await _require_plan(update, "pro"):
        return
    """Mostra e modifica i parametri di rischio.
    Uso:
      /rischio_settings                      → mostra parametri
      /rischio_settings max_leverage 15      → imposta leva max a 15x
      /rischio_settings max_positions 8      → max 8 posizioni simultanee
      /rischio_settings max_pct_per_trade 3  → max 3% capitale per trade
    """
    args = context.args

    if not args:
        r = _risk_params
        lines = [
            "⚙️ *PARAMETRI DI RISCHIO*\n",
            f"  📊 Leverage massimo:      `{r['max_leverage']:.0f}x`",
            f"  📂 Max posizioni simult.: `{r['max_positions']}`",
            f"  💰 Max % capitale/trade:  `{r['max_pct_per_trade']:.1f}%`",
            "",
            "_Usa: /rischio_settings <param> <valore>_",
            "_Es:  /rischio_settings max_leverage 15_",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    if len(args) < 2:
        await update.message.reply_text("Uso: /rischio_settings <parametro> <valore>")
        return

    param = args[0].lower()
    try:
        value = float(args[1])
    except ValueError:
        await update.message.reply_text("❌ Valore non valido.")
        return

    valid = {"max_leverage", "max_positions", "max_pct_per_trade"}
    if param not in valid:
        await update.message.reply_text(
            f"❌ Parametro sconosciuto: `{param}`\n"
            "Disponibili: max_leverage, max_positions, max_pct_per_trade",
            parse_mode="Markdown"
        )
        return

    old = _risk_params[param]
    if param == "max_positions":
        _risk_params[param] = int(value)
    else:
        _risk_params[param] = value

    await update.message.reply_text(
        f"✅ *{param}* aggiornato\n"
        f"  {old} → `{_risk_params[param]}`",
        parse_mode="Markdown"
    )


def get_risk_params() -> dict:
    """Restituisce i parametri di rischio correnti."""
    return dict(_risk_params)




# ───────────────────────────────────────────────────────────────────────────
# CHIUSURA POSIZIONI — by Maintenance Margin e by PnL
# ───────────────────────────────────────────────────────────────────────────

async def cmd_chiudi_mm(update, context):
    """Chiude posizioni con MM% sotto la soglia (/chiudi_mm [soglia%])"""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    args = context.args
    threshold = 15.0
    if args:
        try:
            threshold = float(args[0])
        except ValueError:
            await update.message.reply_text("❌ Uso: /chiudi_mm [soglia%]\nEs: /chiudi_mm 10")
            return
    msg = await update.message.reply_text(
        f"⏳ Chiusura posizioni con MM < {threshold}%…"
    )
    result = await close_positions_by_mm(threshold)
    if not result["closed"] and not result["errors"]:
        await msg.edit_text(f"✅ Nessuna posizione con MM < {threshold}%")
        return
    lines = [f"🔴 Chiusura posizioni MM < {threshold}%"]
    for r in result["closed"]:
        lines.append(f"  ✅ {r['symbol']} {r['side']} — MM {r['mm_pct']:.1f}%")
    for e in result["errors"]:
        lines.append(f"  ❌ {e['symbol']}: {e['error']}")
    await msg.edit_text("\n".join(lines))


async def cmd_chiudi_pnl(update, context):
    """Chiude posizioni in base al PnL totale (/chiudi_pnl [soglia_negativa] [soglia_positiva])"""
    args = context.args
    neg_threshold = -5.0   # chiudi se PnL totale < -5 USDT
    pos_threshold = None   # opzionale: chiudi se PnL totale > X USDT
    if len(args) >= 1:
        try:
            neg_threshold = float(args[0])
        except ValueError:
            await update.message.reply_text(
                "❌ Uso: /chiudi_pnl [soglia_neg] [soglia_pos]\nEs: /chiudi_pnl -10 50"
            )
            return
    if len(args) >= 2:
        try:
            pos_threshold = float(args[1])
        except ValueError:
            pass
    msg = await update.message.reply_text(
        f"⏳ Analisi PnL posizioni (neg < {neg_threshold}, pos > {pos_threshold})…"
    )
    result = await close_positions_by_pnl(neg_threshold, pos_threshold)
    if not result["closed"] and not result["errors"] and not result.get("summary"):
        await msg.edit_text("✅ Nessuna posizione fuori soglia")
        return
    lines = [f"📊 Chiusura posizioni per PnL"]
    total_pnl = result.get("total_pnl", 0)
    lines.append(f"PnL totale portfolio: {total_pnl:+.2f} USDT")
    for r in result["closed"]:
        lines.append(f"  ✅ {r['symbol']} {r['side']} PnL {r['pnl']:+.2f} USDT")
    for e in result["errors"]:
        lines.append(f"  ❌ {e['symbol']}: {e['error']}")
    await msg.edit_text("\n".join(lines))


async def deletekeys_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Elimina le credenziali Bybit dell'utente dal bot."""
    chat_id = update.effective_chat.id
    if user_store.delete(chat_id):
        session_manager.remove_session(chat_id)
        await update.message.reply_text(
            "🗑️ *Credenziali eliminate.*\n\n"
            "Le tue API Key e Secret sono state rimosse.\n"
            "Usa /start per configurarne di nuove.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "ℹ️ Nessuna credenziale trovata per questo account.",
        )


# ══════════════════════════════════════════════════════════════════════════════
# /upgrade — Acquista piano Pro/Elite con crypto
# /plan    — Visualizza piano attuale e scadenza
# ══════════════════════════════════════════════════════════════════════════════

# Stati ConversationHandler upgrade
(
    UPG_PLAN,
    UPG_BILLING,
    UPG_CURRENCY,
    UPG_EMAIL,
) = range(100, 104)


def _kb_plans() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Pro",   callback_data="upg_plan_pro")],
        [InlineKeyboardButton("👑 Elite", callback_data="upg_plan_elite")],
        [InlineKeyboardButton("❌ Cancel", callback_data="upg_cancel")],
    ])


def _kb_billing() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Monthly Recurring", callback_data="upg_bill_recurring")],
        [InlineKeyboardButton("1️⃣ One-Shot 30 days",  callback_data="upg_bill_oneshot")],
        [InlineKeyboardButton("⬅️ Back",               callback_data="upg_back_plan")],
    ])


def _kb_currencies() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 USDT (TRC20 — cheapest)",  callback_data="upg_cur_usdttrc20")],
        [InlineKeyboardButton("💵 USDT (SOL network)",       callback_data="upg_cur_usdtsol")],
        [InlineKeyboardButton("₿  Bitcoin (BTC)",            callback_data="upg_cur_btc")],
        [InlineKeyboardButton("Ξ  Ethereum (ETH)",           callback_data="upg_cur_eth")],
        [InlineKeyboardButton("◎  Solana (SOL)",             callback_data="upg_cur_sol")],
        [InlineKeyboardButton("⬡  BNB (BSC)",                callback_data="upg_cur_bnbbsc")],
        [InlineKeyboardButton("💎 TON",                      callback_data="upg_cur_ton")],
        [InlineKeyboardButton("⬅️ Back",                     callback_data="upg_back_billing")],
    ])


PLAN_FEATURES = {
    "pro": (
        "⚡ *Pro Plan*\n\n"
        "✅ Auto-trading on all exchanges\n"
        "✅ Pre-settlement alerts\n"
        "✅ Backtest\n"
        "✅ Custom thresholds\n"
        "✅ Priority support\n"
    ),
    "elite": (
        "👑 *Elite Plan*\n\n"
        "✅ Everything in Pro\n"
        "✅ Multi-account trading\n"
        "✅ Advanced risk management\n"
        "✅ API access\n"
        "✅ Dedicated support\n"
    ),
}

PLAN_PRICES = {
    "pro":   {"recurring": 20, "oneshot": 25},
    "elite": {"recurring": 45, "oneshot": 55},
}


async def cmd_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "🚀 *Upgrade FundShot*\n\n"
        "Choose the plan you want to activate:",
        parse_mode="Markdown",
        reply_markup=_kb_plans(),
    )
    return UPG_PLAN


async def upgrade_plan_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "upg_cancel":
        await query.edit_message_text("❌ Upgrade cancelled.")
        return ConversationHandler.END

    if data.startswith("upg_plan_"):
        plan = data.replace("upg_plan_", "")
        context.user_data["upg_plan"] = plan
        prices = PLAN_PRICES[plan]
        await query.edit_message_text(
            PLAN_FEATURES[plan] +
            f"\n💰 *Recurring:* ${prices['recurring']}/month\n"
            f"💰 *One-Shot:*  ${prices['oneshot']} / 30 days\n\n"
            "Choose billing type:",
            parse_mode="Markdown",
            reply_markup=_kb_billing(),
        )
        return UPG_BILLING

    return UPG_PLAN


async def upgrade_billing_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "upg_back_plan":
        await query.edit_message_text(
            "🚀 *Upgrade FundShot*\n\nChoose the plan:",
            parse_mode="Markdown",
            reply_markup=_kb_plans(),
        )
        return UPG_PLAN

    if data.startswith("upg_bill_"):
        billing = data.replace("upg_bill_", "")
        context.user_data["upg_billing"] = billing
        plan    = context.user_data.get("upg_plan", "pro")
        price   = PLAN_PRICES[plan][billing]
        label   = "🔄 Monthly Recurring" if billing == "recurring" else "1️⃣ One-Shot 30 days"

        if billing == "recurring":
            # Per il recurring serve l'email — NOWPayments invia invoice automatiche
            await query.edit_message_text(
                f"💳 *{plan.capitalize()} — {label}*\n"
                f"Amount: `${price} USD/month`\n\n"
                "📧 To set up automatic renewal, enter your email address.\n"
                "NOWPayments will send you a monthly invoice automatically.\n\n"
                "_Send your email or type /skip to pay manually each month:_",
                parse_mode="Markdown",
            )
            return UPG_EMAIL

        await query.edit_message_text(
            f"💳 *{plan.capitalize()} — {label}*\n"
            f"Amount: `${price} USD`\n\n"
            "Choose your crypto:",
            parse_mode="Markdown",
            reply_markup=_kb_currencies(),
        )
        return UPG_CURRENCY

    return UPG_BILLING


async def upgrade_currency_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "upg_back_billing":
        await query.edit_message_text(
            "Choose billing type:",
            parse_mode="Markdown",
            reply_markup=_kb_billing(),
        )
        return UPG_BILLING

    if data.startswith("upg_cur_"):
        currency = data.replace("upg_cur_", "")
        plan     = context.user_data.get("upg_plan", "pro")
        billing  = context.user_data.get("upg_billing", "oneshot")
        chat_id  = update.effective_chat.id

        await query.edit_message_text("⏳ Generating payment address...")

        try:
            from payments import create_payment, create_subscription, currency_display
            from db.supabase_client import save_payment, get_user, save_user_email

            user        = await get_user(chat_id)
            email       = context.user_data.get("upg_email", "")
            base_price  = PLAN_PRICES[plan][billing]
            # Applica sconto referral se presente
            from referral import get_discount_for_user, apply_discount
            discount_pct = await get_discount_for_user(user.id) if user else 0
            price_label  = apply_discount(base_price, discount_pct)
            if discount_pct > 0:
                await query.edit_message_text(
                    f"\U0001F381 *{int(discount_pct)}% referral discount applied!*\n"
                    f"Original: ~~${base_price}~~ -> *${price_label}*",
                    parse_mode="Markdown",
                )
                import asyncio; await asyncio.sleep(1.5)
            cur_label   = currency_display(currency)
            billing_lbl = "🔄 Recurring" if billing == "recurring" else "1️⃣ One-Shot"
            plan_lbl    = plan.capitalize()

            # ── Recurring con email → NOWPayments subscription ────────────────
            if billing == "recurring" and email:
                # Salva email su Supabase
                if user:
                    await save_user_email(user.id, email)

                sub = create_subscription(email=email, plan=plan)

                # Salva subscription su Supabase
                if user:
                    await save_payment(
                        user_id=user.id,
                        chat_id=chat_id,
                        nowpay_id=sub["subscription_id"],
                        plan=plan,
                        billing_type=billing,
                        amount_usd=price_label,
                        currency="subscription",
                        status="pending",
                    )

                msg = (
                    f"✅ *{plan_lbl} Subscription Created!*\n\n"
                    f"📧 An invoice has been sent to:\n`{email}`\n\n"
                    f"💰 Amount: `${price_label}/month`\n"
                    f"🔄 Renewal: automatic every 30 days\n\n"
                    f"Click the link in the email to complete the first payment.\n"
                    f"Your plan activates automatically after confirmation.\n\n"
                    f"_Subscription ID: `{sub['subscription_id']}`_"
                )
                await query.edit_message_text(msg, parse_mode="Markdown")

            else:
                # ── One-shot o recurring senza email → pagamento diretto ──────
                result = create_payment(
                    chat_id=chat_id,
                    plan=plan,
                    billing_type=billing,
                    currency=currency,
                )

                if user:
                    await save_payment(
                        user_id=user.id,
                        chat_id=chat_id,
                        nowpay_id=str(result["payment_id"]),
                        plan=plan,
                        billing_type=billing,
                        amount_usd=result["amount_usd"],
                        currency=currency,
                        pay_address=result["pay_address"],
                        pay_amount=result.get("pay_amount", 0),
                        status="pending",
                    )

                msg = (
                    f"💳 *{plan_lbl} — {billing_lbl}*\n\n"
                    f"Send exactly:\n"
                    f"`{result['pay_amount']} {result['pay_currency']}`\n\n"
                    f"To this address:\n"
                    f"`{result['pay_address']}`\n\n"
                    f"💵 ≈ ${price_label} USD\n"
                    f"🪙 Network: {cur_label}\n"
                    f"⏱ Payment expires in ~20 minutes\n\n"
                    f"✅ Your plan will be activated *automatically* after confirmation.\n"
                    f"_Payment ID: `{result['payment_id']}`_"
                )
                await query.edit_message_text(msg, parse_mode="Markdown")

        except Exception as e:
            await query.edit_message_text(
                f"❌ Error generating payment: {e}\n\nPlease try again or contact support@fundshot.app"
            )

        return ConversationHandler.END

    return UPG_CURRENCY


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra piano attuale, scadenza e stato."""
    from db.supabase_client import get_user
    from datetime import datetime, timezone

    chat_id = update.effective_chat.id
    user    = await get_user(chat_id)

    if not user:
        await update.message.reply_text("⚠️ Use /start to register first.")
        return

    plan = user.plan
    plan_emoji = {"free": "🆓", "pro": "⚡", "elite": "👑"}.get(plan, "🆓")
    plan_label = plan.capitalize()

    # Leggi scadenza e billing_type da Supabase raw
    try:
        from db.supabase_client import get_client
        db  = get_client()
        res = db.table("users").select("plan_expires_at,billing_type").eq("id", user.id).single().execute()
        raw = res.data or {}
        expires_at   = raw.get("plan_expires_at")
        billing_type = raw.get("billing_type")
    except Exception:
        expires_at   = None
        billing_type = None

    lines = [f"{plan_emoji} *Your FundShot Plan*\n"]
    lines.append(f"Plan: *{plan_label}*")

    if plan == "free":
        lines += [
            "",
            "Upgrade to unlock:",
            "⚡ Auto-trading",
            "📊 Pre-settlement alerts",
            "🔧 Custom thresholds",
            "",
            "Use /upgrade to activate Pro or Elite.",
        ]
    else:
        if expires_at:
            try:
                exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                days_left = (exp - now).days
                exp_str   = exp.strftime("%d/%m/%Y")
                status_emoji = "✅" if days_left > 3 else "⚠️"
                lines.append(f"Expires: `{exp_str}` ({days_left}d left) {status_emoji}")
            except Exception:
                lines.append(f"Expires: `{expires_at}`")

        if billing_type:
            b_label = "🔄 Monthly Recurring" if billing_type == "recurring" else "1️⃣ One-Shot"
            lines.append(f"Billing: {b_label}")

        lines += ["", "Use /upgrade to renew or change plan."]

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def receive_upgrade_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Riceve l'email per il recurring billing."""
    text = update.message.text.strip()

    # /skip — procede senza email (pagamento manuale ogni mese)
    if text.lower() in ("/skip", "skip"):
        context.user_data["upg_email"] = ""
        await update.message.reply_text(
            "No problem — you can renew manually each month with /upgrade.\n\n"
            "Choose your crypto:",
            reply_markup=_kb_currencies(),
        )
        return UPG_CURRENCY

    # Validazione email base
    import re
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", text):
        await update.message.reply_text(
            "❌ Invalid email. Please send a valid email address, or type /skip to continue without it.",
        )
        return UPG_EMAIL

    context.user_data["upg_email"] = text
    plan    = context.user_data.get("upg_plan", "pro")
    billing = context.user_data.get("upg_billing", "recurring")
    price   = PLAN_PRICES[plan][billing]

    await update.message.reply_text(
        f"✅ Email saved: `{text}`\n\n"
        f"Choose your crypto to pay `${price}/month`:",
        parse_mode="Markdown",
        reply_markup=_kb_currencies(),
    )
    return UPG_CURRENCY


def build_upgrade_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("upgrade", cmd_upgrade)],
        states={
            UPG_PLAN:     [CallbackQueryHandler(upgrade_plan_cb,     pattern="^upg_")],
            UPG_BILLING:  [CallbackQueryHandler(upgrade_billing_cb,  pattern="^upg_")],
            UPG_EMAIL:    [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_upgrade_email),
                           CommandHandler("skip", receive_upgrade_email)],
            UPG_CURRENCY: [CallbackQueryHandler(upgrade_currency_cb, pattern="^upg_")],
        },
        fallbacks=[CommandHandler("upgrade", cmd_upgrade)],
        per_chat=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# /trading — Statistiche auto-trader (wrapper verso bot.py cmd_stats)
# /aperte  — Posizioni aperte auto-trader (wrapper verso bot.py cmd_posizioni_trader)
# Queste funzioni vengono iniettate da bot.py tramite inject_bot_commands()
# ══════════════════════════════════════════════════════════════════════════════

_bot_cmd_trading  = None
_bot_cmd_aperte   = None

def inject_bot_commands(cmd_trading_fn, cmd_aperte_fn):
    """Chiamato da bot.py per iniettare i comandi che richiedono accesso a _funding_trader."""
    global _bot_cmd_trading, _bot_cmd_aperte
    _bot_cmd_trading = cmd_trading_fn
    _bot_cmd_aperte  = cmd_aperte_fn

async def cmd_trading(update, context):
    """Statistiche auto-trading — delega a bot.py."""
    if not await _require_plan(update, "pro"):
        return
    if _bot_cmd_trading:
        await _bot_cmd_trading(update, context)
    else:
        await update.message.reply_text("⚠️ Auto-trading non inizializzato.")

async def cmd_aperte(update, context):
    """Posizioni auto-trader aperte — delega a bot.py."""
    if not await _require_plan(update, "pro"):
        return
    if _bot_cmd_aperte:
        await _bot_cmd_aperte(update, context)
    else:
        await update.message.reply_text("⚠️ Auto-trading non inizializzato.")


def register(app):
    """Registra tutti i command handler sull'applicazione Telegram."""
    # Nota: /start è gestito da onboarding.build_onboarding_handler() in bot.py

    # ── Funding Rate ─────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("top10",      top10))
    app.add_handler(CommandHandler("storico",    storico))      # /storico SYM [7g]
    app.add_handler(CommandHandler("backtest",   backtest_cmd))

    # ── Account ───────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("saldo",      saldo))
    app.add_handler(CommandHandler("posizioni",  posizioni))
    app.add_handler(CommandHandler("rischio",    rischio))
    app.add_handler(CommandHandler("summary",    summary))

    # ── Auto-Trading ──────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("trading",    cmd_trading))  # stats bot
    app.add_handler(CommandHandler("aperte",     cmd_aperte))   # posizioni bot

    # ── Watchlist & Alert ─────────────────────────────────────────────────────
    app.add_handler(CommandHandler("watchlist",  watchlist_cmd))
    app.add_handler(CommandHandler("watch",      watch_cmd))
    app.add_handler(CommandHandler("unwatch",    unwatch_cmd))
    app.add_handler(CommandHandler("mute",       mute_cmd))
    app.add_handler(CommandHandler("unmute",     unmute_cmd))
    app.add_handler(CommandHandler("alerts",     alerts_cmd))

    # ── Sistema ───────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("help",       help_cmd))
    app.add_handler(CommandHandler("status",     status_cmd))
    app.add_handler(CommandHandler("test",       test_cmd))
    app.add_handler(CommandHandler("deletekeys", deletekeys_cmd))

    # ── Referral ──────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("alertfilter", cmd_alertfilter))
    app.add_handler(CommandHandler("referral",    cmd_referral))
    app.add_handler(CommandHandler("setwallet",   cmd_setwallet))
    app.add_handler(CommandHandler("addinf",      cmd_addinf))
    app.add_handler(CommandHandler("payoutlist",  cmd_payoutlist))
    app.add_handler(CommandHandler("clearpayouts", cmd_clearpayouts))


# ══════════════════════════════════════════════════════════════════════════════
# /referral — Mostra link referral e statistiche
# /setwallet — Imposta wallet USDT per payout
# /addinf — Admin: promuove utente a influencer
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra il link referral personale e le statistiche."""
    import os
    from db.supabase_client import get_user, get_or_create_referral_code
    from referral import get_referral_info, REFERRAL_COMMISSION_PCT, PAYOUT_MIN_USD

    chat_id = update.effective_chat.id
    user    = await get_user(chat_id)
    if not user:
        await update.message.reply_text("⚠️ Use /start to register first.")
        return

    code = await get_or_create_referral_code(user.id, chat_id)
    info = await get_referral_info(user.id)

    bot_username = (await context.bot.get_me()).username
    ref_link     = f"https://t.me/{bot_username}?start=ref_{code}"

    is_inf = info.get("is_influencer", False)
    inf_link = f"https://t.me/{bot_username}?start=inf_{code}" if is_inf else None

    lines = [
        f"{'👑' if is_inf else '🔗'} *{'Influencer' if is_inf else 'Referral'} Program*\n",
        f"Your referral link:",
        f"`{ref_link}`\n",
    ]

    if is_inf:
        lines += [
            f"🎁 Your influencer link:",
            f"`{inf_link}`",
            f"_(users who join via this link get 5% off forever)_\n",
        ]

    lines += [
        f"📊 *Stats:*",
        f"  Invited: `{info['total_invited']}`",
        f"  Converted (paid): `{info['converted']}`",
        f"  Pending: `{info['pending']}`\n",
        f"💰 *Earnings:*",
        f"  Balance: `${info['balance']:.2f} USDT`",
        f"  Total earned: `${info['total_earned']:.2f} USDT`\n",
        f"📌 *How it works:*",
        f"  • You earn *{int(REFERRAL_COMMISSION_PCT)}%* on every payment",
        f"  • Including renewals — forever",
        f"  • Payout: automatic monthly (min ${PAYOUT_MIN_USD:.0f})\n",
        f"Set your USDT wallet for payouts:\n`/setwallet YOUR_USDT_TRC20_ADDRESS`",
    ]

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_setwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Imposta il wallet USDT TRC20 per ricevere i payout referral."""
    from db.supabase_client import get_user, save_referral_wallet

    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text(
            "Usage: `/setwallet YOUR_USDT_TRC20_ADDRESS`\n\n"
            "Example: `/setwallet TXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`",
            parse_mode="Markdown",
        )
        return

    wallet = context.args[0].strip()
    # Validazione base TRC20 (inizia con T, 34 caratteri)
    if not (wallet.startswith("T") and len(wallet) == 34):
        await update.message.reply_text(
            "❌ Invalid USDT TRC20 address.\n"
            "TRC20 addresses start with `T` and are 34 characters long.",
            parse_mode="Markdown",
        )
        return

    user = await get_user(chat_id)
    if not user:
        await update.message.reply_text("⚠️ Use /start to register first.")
        return

    await save_referral_wallet(user.id, wallet)
    await update.message.reply_text(
        f"✅ Wallet saved: `{wallet[:6]}...{wallet[-4:]}`\n\n"
        "You'll receive USDT payouts here automatically each month.",
        parse_mode="Markdown",
    )


async def cmd_addinf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin only: promuove un utente a influencer."""
    import os
    from db.supabase_client import get_user, set_influencer

    # Solo owner
    chat_id = str(update.effective_chat.id)
    owner   = str(os.getenv("CHAT_ID", ""))
    if chat_id != owner:
        await update.message.reply_text("⛔ Not authorized.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/addinf @username` or `/addinf CHAT_ID`\n"
            "Remove: `/addinf @username remove`",
            parse_mode="Markdown",
        )
        return

    target  = context.args[0].lstrip("@")
    remove  = len(context.args) > 1 and context.args[1].lower() == "remove"

    # Cerca utente per handle o chat_id
    from db.supabase_client import get_client
    db = get_client()
    try:
        if target.isdigit():
            res = db.table("users").select("*").eq("chat_id", int(target)).single().execute()
        else:
            res = db.table("users").select("*").eq("telegram_handle", target).single().execute()
        u = res.data
    except Exception:
        u = None

    if not u:
        await update.message.reply_text(f"❌ User `{target}` not found.", parse_mode="Markdown")
        return

    await set_influencer(u["id"], not remove)

    action = "removed from" if remove else "added as"
    await update.message.reply_text(
        f"✅ `@{u.get('telegram_handle', target)}` {action} influencer.\n"
        f"Chat ID: `{u['chat_id']}`",
        parse_mode="Markdown",
    )

    # Notifica l'utente
    try:
        if not remove:
            bot_username = (await context.bot.get_me()).username
            from db.supabase_client import get_or_create_referral_code
            code = await get_or_create_referral_code(u["id"], u["chat_id"])
            inf_link = f"https://t.me/{bot_username}?start=inf_{code}"
            await context.bot.send_message(
                chat_id=u["chat_id"],
                text=(
                    "🎉 *You're now a FundShot Influencer!*\n\n"
                    "Your special link gives users *5% off forever*:\n"
                    f"`{inf_link}`\n\n"
                    "You earn *10% commission* on every payment — forever.\n"
                    "Use /referral to see your stats and set your payout wallet."
                ),
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.warning("addinf notify: %s", e)


async def cmd_payoutlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin only: lista dei payout referral da fare questo mese."""
    import os
    from db.supabase_client import get_client

    chat_id = str(update.effective_chat.id)
    owner   = str(os.getenv("CHAT_ID", ""))
    if chat_id != owner:
        await update.message.reply_text("⛔ Not authorized.")
        return

    db = get_client()
    try:
        res = db.table("users").select(
            "chat_id,telegram_handle,referral_balance_usd,referral_wallet_usdt,is_influencer"
        ).gt("referral_balance_usd", 0).order("referral_balance_usd", desc=True).execute()

        users = res.data or []

        if not users:
            await update.message.reply_text("✅ No pending payouts this month.")
            return

        total = sum(float(u.get("referral_balance_usd", 0) or 0) for u in users)
        lines = [f"💸 *Referral Payout List* — {len(users)} pending\n"]

        for u in users:
            balance  = float(u.get("referral_balance_usd", 0) or 0)
            wallet   = u.get("referral_wallet_usdt") or "⚠️ NO WALLET"
            handle   = u.get("telegram_handle") or str(u.get("chat_id"))
            inf_tag  = " 👑" if u.get("is_influencer") else ""
            has_wal  = "✅" if u.get("referral_wallet_usdt") else "❌"

            lines.append(
                f"{has_wal} @{handle}{inf_tag}\n"
                f"   Amount: `${balance:.4f} USDT`\n"
                f"   Wallet: `{wallet}`"
            )

        lines += [
            f"\n{'─'*20}",
            f"💰 Total to send: `${total:.4f} USDT`",
            "",
            "After sending, use /clearpayouts to reset balances.",
        ]

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def cmd_clearpayouts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin only: azzera i balance referral dopo aver fatto i pagamenti."""
    import os
    from db.supabase_client import get_client

    chat_id = str(update.effective_chat.id)
    owner   = str(os.getenv("CHAT_ID", ""))
    if chat_id != owner:
        await update.message.reply_text("⛔ Not authorized.")
        return

    db = get_client()
    try:
        res = db.table("users").select("id,chat_id,telegram_handle,referral_balance_usd").gt(
            "referral_balance_usd", 0
        ).execute()
        users = res.data or []

        if not users:
            await update.message.reply_text("Nothing to clear.")
            return

        # Azzera balance di tutti
        for u in users:
            db.table("users").update({"referral_balance_usd": 0.0}).eq("id", u["id"]).execute()

        # Notifica ogni utente
        for u in users:
            try:
                bal = float(u.get("referral_balance_usd", 0) or 0)
                await context.bot.send_message(
                    chat_id=u["chat_id"],
                    text=(
                        f"💸 *Referral Payout Sent!*\n\n"
                        f"Amount: `${bal:.4f} USDT`\n\n"
                        f"Check your wallet — payment sent this month.\n"
                        f"Keep referring to earn more! /referral"
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                pass

        await update.message.reply_text(
            f"✅ Cleared {len(users)} balances.\n"
            f"All users notified of their payout."
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def cmd_alertfilter(update, context):
    """Filter alerts by minimum level and per-symbol cooldown."""
    from alert_logic import set_user_min_level, set_user_cooldown, get_user_alert_prefs
    cid_str = str(update.effective_chat.id)
    prefs   = get_user_alert_prefs(cid_str)
    names   = {0: "All (SOFT+)", 1: "HIGH+", 2: "EXTREME+", 3: "HARD+", 4: "JACKPOT only"}

    if not context.args:
        await update.message.reply_text(
            "*Alert Filter*\n\n"
            f"Min level: *{names.get(prefs['min_level'], '?')}*\n"
            f"Cooldown: *{prefs['cooldown_min']} min* per symbol\n\n"
            "*Set level:*\n"
            "`/alertfilter level 0` - All alerts\n"
            "`/alertfilter level 1` - HIGH and above\n"
            "`/alertfilter level 2` - EXTREME and above\n"
            "`/alertfilter level 3` - HARD and above\n"
            "`/alertfilter level 4` - JACKPOT only\n\n"
            "*Set cooldown:*\n"
            "`/alertfilter cooldown 5` - 5 min between same symbol\n"
            "`/alertfilter cooldown 30` - 30 min (less noise)",
            parse_mode="Markdown",
        )
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/alertfilter level 2` or `/alertfilter cooldown 10`", parse_mode="Markdown")
        return

    param = context.args[0].lower()
    try:
        value = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Value must be a number.")
        return

    if param == "level":
        set_user_min_level(cid_str, value)
        await update.message.reply_text(
            f"Filter set: *{names.get(value, str(value))}*\nOnly alerts at or above this level will be sent.",
            parse_mode="Markdown",
        )
    elif param == "cooldown":
        set_user_cooldown(cid_str, value)
        await update.message.reply_text(
            f"Cooldown set: *{value} min* per symbol.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("Unknown param. Use `level` or `cooldown`.", parse_mode="Markdown")
