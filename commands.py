"""
commands.py â Funding King Bot
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

logger = logging.getLogger(__name__)

# ââ ConversationHandler states ââââââââââââââââââââââââââââââââââââââââââââââââ
MENU, WAITING_API_KEY, WAITING_API_SECRET = range(3)


def is_watched(symbol: str) -> bool:
    """Proxy verso watchlist_manager â usato da bot.py."""
    return wm.is_watched(symbol)


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# /start â Setup Wizard
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def _has_credentials(chat_id: int | str | None = None) -> bool:
    """Verifica credenziali: per-utente se chat_id Ã¨ fornito, globale come fallback."""
    if chat_id is not None:
        return user_store.has_credentials(chat_id)
    # Fallback legacy: controlla variabili d'ambiente globali
    return bool(os.getenv("BYBIT_API_KEY")) and bool(os.getenv("BYBIT_API_SECRET"))


def _build_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ð Imposta API Key", callback_data="set_api_key")],
        [InlineKeyboardButton("ð Imposta API Secret", callback_data="set_api_secret")],
        [InlineKeyboardButton("â Conferma e Avvia", callback_data="confirm_start")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id

    if _has_credentials(chat_id):
        key_masked = _mask(user_store.get_api_key(chat_id))
        await update.message.reply_text(
            "ð¤ *Funding King Bot* â Attivo â\n\n"
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
        "ð¤ *Funding King Bot â Setup*\n\n"
        f"Chat ID: `{chat_id}` â (rilevato automaticamente)\n"
        f"API Key: `{key or 'â ï¸ non impostata'}`\n"
        f"API Secret: `{secret or 'â ï¸ non impostato'}`\n\n"
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
            "ð Invia la tua *Bybit API Key* (il messaggio verrÃ  eliminato automaticamente):",
            parse_mode="Markdown",
        )
        return WAITING_API_KEY

    if data == "set_api_secret":
        await query.edit_message_text(
            "ð Invia il tuo *Bybit API Secret* (il messaggio verrÃ  eliminato automaticamente):",
            parse_mode="Markdown",
        )
        return WAITING_API_SECRET

    if data == "confirm_start":
        chat_id = query.from_user.id
        if not _has_credentials(chat_id):
            await query.edit_message_text(
                "â ï¸ Configura prima API Key e API Secret.",
                reply_markup=_build_menu_keyboard(),
            )
            return MENU
        session_manager.reload_session(chat_id)
        # Test connessione
        try:
            sess = session_manager.get_session(chat_id)
            test = await sess.test_connection()
            conn_status = "â Connessione Bybit OK" if test.get("ok") else f"â ï¸ {test.get('error','errore')}"
        except Exception as e:
            conn_status = f"â ï¸ Errore test: {e}"
        await query.edit_message_text(
            "â *Configurazione completata!*\n\n"
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
        f"â API Key salvata: `{_mask(value)}`",
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
        f"â API Secret salvato: `{_mask(value)}`",
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


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# /help
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 *FUNDING KING BOT — Comandi disponibili*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 *FUNDING RATE*\n"
        "/funding\_top — Top 10 funding positivi (SHORT)\n"
        "/funding\_bottom — Top 10 funding negativi (LONG)\n"
        "/top10 — Classifica 10 SHORT + 10 LONG in tempo reale\n"
        "/storico `<SIMBOLO>` — Ultimi 8 cicli\n"
        "/storico7g `<SIMBOLO>` — Storico 7 giorni con grafici\n"
        "/backtest `<SYM|top10|watchlist>` — Simula P&L 30 giorni\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "💼 *ACCOUNT (sola lettura)*\n"
        "/saldo — Saldo wallet Bybit\n"
        "/posizioni — Posizioni aperte con PnL%\n"
        "/rischio — Analisi rischio posizioni aperte\n"
        "/summary — Riepilogo rapido wallet + posizioni\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 *WATCHLIST & NOTIFICHE*\n"
        "/watchlist — Stato completo watchlist\n"
        "/watch `<SYM>` — Aggiungi simboli (es. `BTC ETH SOL`)\n"
        "/unwatch `<SYM>` — Rimuovi | `/unwatch all` per reset\n"
        "/mute `<SYM>` — Silenzia simbolo\n"
        "/unmute `<SYM>` — Riattiva simbolo\n"
        "/alerts — Soglie custom per simbolo\n"
        "/alerts `<SYM> <livello> <valore>` — Imposta soglia\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🔧 *SISTEMA*\n"
        "/start — Setup / configurazione credenziali\n"
        "/status — Stato bot e credenziali\n"
        "/test — Test connessione Bybit\n"
        "/analytics — Posizioni aperte + storico alert\n"
        "/help — Questo messaggio\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🔔 *Modalità: ALERT ONLY*\n"
        "Il bot monitora i funding rate e invia notifiche.\n"
        "Nessuna operazione di trading viene eseguita.\n\n"
        "📡 *Alert automatici ogni 60s:*\n"
        "🔴 HARD ≥ ±2% | 🔥 EXTREME ≥ ±1.5%\n"
        "🚨 HIGH ≥ ±1% | ℹ️ CHIUSURA ≥ ±0.23%\n"
        "✅ RIENTRO ≤ ±0.75% | ⏰ Prossimo funding\n"
        "📈 PUMP/📉 DUMP ≥ ±5% in 1H\n"
        "🧨 Liquidazioni ≥ $100k"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


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
    tok = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("CHAT_ID", "—")

    text = (
        "🤖 *FUNDING KING BOT — Status*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🔌 *Connessioni*\n"
        f"  Telegram: {'✅' if tok else '❌'}\n"
        f"  Bybit API: {'✅' if has_creds else '❌ Credenziali mancanti'}\n\n"
        "🔑 *Credenziali*\n"
        f"  Token: `{_mask(tok)}`\n"
        f"  Chat ID: `{chat_id}`\n"
        f"  API Key: `{_mask(key) if key else '⚠️ non impostata'}`\n"
        f"  API Secret: `{_mask(secret) if secret else '⚠️ non impostato'}`\n\n"
        "⚙️ *Bot*\n"
        f"  Stato: {'✅ Attivo' if monitoring else '⏸ In attesa'}\n"
        "  Modalità: 🔔 *ALERT ONLY*\n"
        f"  Simboli monitorati: {symbols_count}\n"
        f"  Uptime: {uptime_str}\n"
        f"  Alert inviati: {alerts_sent}\n\n"
        "🕐 *Ultimo ciclo*\n"
        f"  {last_cycle}\n\n"
        "📡 *Simboli in alert ora*\n"
        f"{alert_list}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ð Avvio test connessione Bybit...")
    results = await bc.test_connection()

    p = results.get("public", {})
    a = results.get("auth", {})
    pos = results.get("positions", {})

    total_ms = sum(r.get("latency_ms", 0) for r in results.values() if r.get("latency_ms", 0) > 0)

    pub_line = (
        f"â OK â {p['latency_ms']} ms â {p.get('symbols', '?')} simboli"
        if p.get("ok") else
        f"â FAIL â {p.get('error', '?')} ({p.get('latency_ms', '?')} ms)"
    )
    auth_line = (
        f"â OK â {a['latency_ms']} ms â Equity: ${a.get('equity', 0):,.2f}"
        if a.get("ok") else
        f"â FAIL â {a.get('error', '?')} ({a.get('latency_ms', '?')} ms)"
    )
    pos_line = (
        f"â OK â {pos['latency_ms']} ms â {pos.get('open', 0)} posizioni aperte"
        if pos.get("ok") else
        f"â FAIL â {pos.get('error', '?')}"
    )
    # Aggiungi dettaglio per-categoria se ci sono errori
    detail = pos.get("detail", {})
    detail_lines = []
    for lbl, d in detail.items():
        if isinstance(d, dict):
            code = d.get("retCode", "?")
            msg  = d.get("retMsg", d.get("error", ""))
            nz   = d.get("nonzero", 0)
            icon = "â" if code == 0 else "â ï¸"
            detail_lines.append(f"   {icon} [{lbl}] code={code} pos={nz} {msg[:40] if msg else ''}")
    pos_detail_str = "\n" + "\n".join(detail_lines) if detail_lines else ""

    all_ok = p.get("ok") and a.get("ok") and pos.get("ok")
    summary = "â Tutti i test superati" if all_ok else "â ï¸ Alcuni test falliti"

    text = (
        f"ð§ *TEST CONNESSIONE BYBIT*\n\n"
        f"1ï¸â£ API Pubblica\n   {pub_line}\n\n"
        f"2ï¸â£ API Autenticata\n   {auth_line}\n\n"
        f"3ï¸â£ Posizioni\n   {pos_line}{pos_detail_str}\n\n"
        f"â± Tempo totale: {total_ms} ms\n"
        f"{summary}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# /funding_top & /funding_bottom
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

async def funding_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ð Recupero funding positivi...")
    tickers = await bc.get_funding_tickers()
    tickers_sorted = sorted(
        tickers,
        key=lambda t: float(t.get("fundingRate", 0)),
        reverse=True,
    )[:10]

    if not tickers_sorted:
        await update.message.reply_text("Nessun dato disponibile.")
        return

    lines = ["ð *TOP 10 FUNDING POSITIVI (SHORT)*\n"]
    for i, t in enumerate(tickers_sorted, 1):
        rate = float(t.get("fundingRate", 0)) * 100
        interval = t.get("fundingIntervalHour", "?")
        lines.append(f"{i}. `{t['symbol']}` â *{rate:+.4f}%* ogni {interval}H")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def funding_bottom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ð Recupero funding negativi...")
    tickers = await bc.get_funding_tickers()
    tickers_sorted = sorted(
        tickers,
        key=lambda t: float(t.get("fundingRate", 0)),
    )[:10]

    if not tickers_sorted:
        await update.message.reply_text("Nessun dato disponibile.")
        return

    lines = ["ð *TOP 10 FUNDING NEGATIVI (LONG)*\n"]
    for i, t in enumerate(tickers_sorted, 1):
        rate = float(t.get("fundingRate", 0)) * 100
        interval = t.get("fundingIntervalHour", "?")
        lines.append(f"{i}. `{t['symbol']}` â *{rate:+.4f}%* ogni {interval}H")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# /storico <SIMBOLO>
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

async def storico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Uso: /storico BTCUSDT")
        return
    symbol = args[0].upper()
    await update.message.reply_text(f"ð Storico funding {symbol}...")

    history = await bc.get_funding_history(symbol, limit=8)
    if not history:
        await update.message.reply_text(f"Nessun dato per {symbol}.")
        return

    lines = [f"ð *STORICO FUNDING â {symbol}*\n"]
    for entry in history:
        rate = float(entry.get("fundingRate", 0)) * 100
        ts = int(entry.get("fundingRateTimestamp", 0)) // 1000
        dt = datetime.fromtimestamp(ts, tz=TZ_IT).strftime("%d/%m %H:%M")
        emoji = "ð¢" if rate >= 0 else "ð´"
        lines.append(f"{emoji} {dt} â *{rate:+.4f}%*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# /storico7g <SIMBOLO> â Storico 7 giorni con mini-chart e statistiche
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

# Blocchi unicode per il mini-chart (8 livelli: da quasi zero a massimo)
_BARS = " ââââââââ"


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
    """Freccia di tendenza basata sul confronto prima metÃ  vs seconda metÃ ."""
    if len(rates) < 4:
        return "â¡ï¸"
    mid   = len(rates) // 2
    first = sum(abs(r) for r in rates[:mid]) / mid
    last  = sum(abs(r) for r in rates[mid:]) / (len(rates) - mid)
    if last > first * 1.1:
        return "ð"
    if last < first * 0.9:
        return "ð"
    return "â¡ï¸"


async def storico7g(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "Uso: `/storico7g BTCUSDT`\nMostra storico funding ultimi 7 giorni con statistiche.",
            parse_mode="Markdown",
        )
        return

    symbol = args[0].upper()
    await update.message.reply_text(f"ð Recupero storico 7 giorni â *{symbol}*...", parse_mode="Markdown")

    history = await bc.get_funding_history_7d(symbol)
    if not history:
        await update.message.reply_text(
            f"â Nessun dato trovato per `{symbol}`.\n"
            "Controlla che il simbolo sia corretto (es. `BTCUSDT`).",
            parse_mode="Markdown",
        )
        return

    # Ordina dal meno recente al piÃ¹ recente per il chart
    entries = sorted(history, key=lambda e: int(e.get("fundingRateTimestamp", 0)))
    rates   = [float(e.get("fundingRate", 0)) * 100 for e in entries]
    abs_rates = [abs(r) for r in rates]
    timestamps = [int(e.get("fundingRateTimestamp", 0)) // 1000 for e in entries]

    # ââ Statistiche globali âââââââââââââââââââââââââââââââââââââââââââââââââââ
    avg_rate  = sum(rates) / len(rates)
    avg_abs   = sum(abs_rates) / len(abs_rates)
    max_rate  = max(rates)
    min_rate  = min(rates)
    max_idx   = rates.index(max_rate)
    min_idx   = rates.index(min_rate)
    max_dt    = datetime.fromtimestamp(timestamps[max_idx], tz=TZ_IT).strftime("%d/%m %H:%M")
    min_dt    = datetime.fromtimestamp(timestamps[min_idx], tz=TZ_IT).strftime("%d/%m %H:%M")
    last_rate = rates[-1]  # piÃ¹ recente
    trend     = _trend_emoji(rates)

    # Conta cicli positivi vs negativi
    pos_count = sum(1 for r in rates if r > 0)
    neg_count = sum(1 for r in rates if r < 0)
    neu_count = len(rates) - pos_count - neg_count

    # ââ Mini-chart (max 40 caratteri) âââââââââââââââââââââââââââââââââââââââââ
    # Raggruppa se ci sono troppi punti
    chart_values = abs_rates
    if len(chart_values) > 40:
        # Sottocampiona a 40 punti
        step = len(chart_values) / 40
        chart_values = [chart_values[int(i * step)] for i in range(40)]
    spark = _spark(chart_values)

    # ââ Media per giorno ââââââââââââââââââââââââââââââââââââââââââââââââââââââ
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
        emoji = "ð¢" if day_avg > 0.01 else ("ð´" if day_avg < -0.01 else "âª")
        # Barra visiva proporzionale (max 10 caratteri)
        bar_len = min(10, max(1, int(abs(day_avg) / max(avg_abs, 0.001) * 10)))
        bar = ("â" * bar_len).ljust(10)
        daily_lines.append(
            f"  {day}  {emoji} `{day_avg:+.4f}%`  |{bar}|  ({len(day_rates)} cicli)"
        )

    # ââ Intervallo del simbolo ââââââââââââââââââââââââââââââââââââââââââââââââ
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

    # ââ Composizione messaggio ââââââââââââââââââââââââââââââââââââââââââââââââ
    lines = [
        f"ð *STORICO 7 GIORNI â {symbol}* {trend}",
        "",
        f"*Andamento funding (valore assoluto):*",
        f"`{spark}`",
        f"  â max    â min",
        "",
        "ð *Statistiche globali:*",
        f"  Media (signed):  `{avg_rate:+.4f}%`",
        f"  Media (assoluta):`{avg_abs:+.4f}%`",
        f"  Max:  `{max_rate:+.4f}%`  ({max_dt})",
        f"  Min:  `{min_rate:+.4f}%`  ({min_dt})",
        f"  Attuale (ultimo): `{last_rate:+.4f}%`",
        "",
        f"  ð¢ Positivi: {pos_count}  ð´ Negativi: {neg_count}  âª Neutri: {neu_count}",
        "",
        "ð *Media giornaliera:*",
    ] + daily_lines + [
        "",
        f"â± Intervallo: {interval_str}  |  Cicli analizzati: {len(rates)}",
    ]

    # Telegram ha limite 4096 caratteri per messaggio
    msg = "\n".join(lines)
    if len(msg) > 4000:
        # Invia in due parti
        split = lines.index("ð *Media giornaliera:*")
        await update.message.reply_text("\n".join(lines[:split]), parse_mode="Markdown")
        await update.message.reply_text("\n".join(lines[split:]), parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# /saldo
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

async def saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _has_credentials():
        await update.message.reply_text("â ï¸ Configura le credenziali con /start.")
        return
    await update.message.reply_text("ð¼ Recupero saldo...")

    wallet = await bc.get_wallet_balance()
    if not wallet:
        await update.message.reply_text("â Impossibile recuperare il saldo. Controlla le API key con /test.")
        return

    pnl_emoji = "â" if wallet["totalPerpUPL"] >= 0 else "â"
    lines = [
        "ð¼ *SALDO ACCOUNT â Bybit*\n",
        f"Equity totale:      `${wallet['totalEquity']:>12,.2f}`",
        f"Wallet balance:     `${wallet['totalWalletBalance']:>12,.2f}`",
        f"Margine disponibile:`${wallet['totalAvailableBalance']:>12,.2f}`",
        f"Margine impegnato:  `${wallet['totalInitialMargin']:>12,.2f}`",
        f"PnL aperto:         `${wallet['totalPerpUPL']:>+12,.2f}` {pnl_emoji}",
        "",
        "ðª *Saldi per coin:*",
    ]
    for c in wallet["coins"]:
        lines.append(f"  {c['coin']}: `{c['walletBalance']:,.4f}` (â ${c['usdValue']:,.2f})")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# /posizioni
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

async def posizioni(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _has_credentials():
        await update.message.reply_text("â ï¸ Configura le credenziali con /start.")
        return
    await update.message.reply_text("ð Recupero posizioni...")

    positions = await bc.get_positions()
    if not positions:
        # Esegui diagnostica veloce per capire il motivo
        diag = await bc.test_positions_api()
        diag_lines = ["ð­ *Nessuna posizione aperta trovata.*"]
        diag_lines.append("")
        diag_lines.append("ð *Diagnostica API:*")
        all_ok = True
        for lbl, d in diag.items():
            if isinstance(d, dict):
                code = d.get("retCode", "?")
                msg  = d.get("retMsg", d.get("error", ""))
                nz   = d.get("nonzero", 0)
                icon = "â" if code == 0 else "â ï¸"
                if code != 0:
                    all_ok = False
                diag_lines.append(f"  {icon} `{lbl}` â code={code}, pos={nz}")
                if code != 0 and msg:
                    diag_lines.append(f"     _{msg[:60]}_")
        if all_ok:
            diag_lines.append("")
            diag_lines.append("â¹ï¸ L'API risponde correttamente â le posizioni sono realmente vuote su questo account.")
            diag_lines.append("ð¡ Se hai posizioni aperte, verifica che le API Key appartengano all'account corretto.")
        await update.message.reply_text("\n".join(diag_lines), parse_mode="Markdown")
        return

    lines = ["ð *POSIZIONI APERTE â Bybit*\n"]
    total_pnl = 0.0

    for i, p in enumerate(positions, 1):
        side_emoji = "ð¢" if p["side"] == "Buy" else "ð´"
        direction = "LONG" if p["side"] == "Buy" else "SHORT"
        pnl = p["unrealisedPnl"]
        pnl_pct = p["pnlPct"]
        total_pnl += pnl
        pnl_emoji = "â" if pnl >= 0 else "â"
        status = "â ï¸ Liquidazione!" if p["positionStatus"] == "Liq" else ""

        block = [
            f"{i}) *{p['symbol']}* {side_emoji} {direction} x{p['leverage']}",
            f"   Size: `{p['size']}`",
            f"   Entry: `{p['avgPrice']:,.2f} $`",
            f"   Mark:  `{p['markPrice']:,.2f} $`",
            f"   PnL:   `{pnl:+,.2f} $` ({pnl_pct:+.1f}%) {pnl_emoji}",
            f"   Liq:   `{p['liqPrice']:,.2f} $` {status}",
        ]
        if p["takeProfit"]:
            block.append(f"   TP:    `{p['takeProfit']:,.2f} $`")
        if p["stopLoss"]:
            block.append(f"   SL:    `{p['stopLoss']:,.2f} $`")
        block.append("")
        lines.extend(block)

    total_emoji = "â" if total_pnl >= 0 else "â"
    lines.append(f"âââââââââââââââââââââ")
    lines.append(f"Totale PnL aperto: `{total_pnl:+,.2f} $` {total_emoji}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# Watchlist persistente: /watch /unwatch /mute /unmute /watchlist /alerts
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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
        lines = [f"â *Watchlist aggiornata* ({len(wl)} simboli)\n"]
        for s in sorted(wl):
            alert_state = al._state.get(s, {}).get("level", "none")
            badge = " ð´" if alert_state != "none" else ""
            custom = wm.get_all_custom_thresholds().get(s)
            custom_tag = " âï¸" if custom else ""
            muted = "ð" if s in wm.get_muted() else ""
            lines.append(f"  â¢ `{s}`{badge}{custom_tag}{muted}")
    else:
        lines = []

    if unknown:
        lines.append(f"\nâ ï¸ Non trovati su Bybit: `{'`, `'.join(unknown)}`")

    if not valid and not unknown:
        lines = ["â ï¸ Nessun simbolo valido specificato."]

    lines.append("\n_âï¸ = soglie custom  ð´ = in alert  ð = silenziato_")
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
            "â Watchlist svuotata. Il bot monitora ora *tutti* i simboli.",
            parse_mode="Markdown",
        )
        return

    raw     = context.args
    symbols = [s.upper() if s.upper().endswith("USDT") else s.upper() + "USDT" for s in raw]
    removed = wm.remove_symbols(symbols)
    not_found = [s for s in symbols if s not in removed]

    lines = []
    if removed:
        lines.append(f"â Rimossi: `{'`, `'.join(removed)}`")
    if not_found:
        lines.append(f"â ï¸ Non erano in watchlist: `{'`, `'.join(not_found)}`")

    wl = wm.get_watchlist()
    if wl:
        lines.append(f"\nWatchlist: {', '.join(f'`{s}`' for s in sorted(wl))}")
    else:
        lines.append("\nWatchlist vuota â monitor *tutti* i simboli.")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        muted = wm.get_muted()
        msg = (
            f"ð *Simboli silenziati:* {', '.join(f'`{s}`' for s in sorted(muted))}"
            if muted else
            "ð *Nessun simbolo silenziato.*\n*Uso:* `/mute BTCUSDT`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    known   = await _get_known_symbols()
    valid, unknown = wm.validate_symbols(context.args, known)
    added   = wm.mute_symbols(valid)

    lines = []
    if added:
        lines.append(f"ð Silenziati: `{'`, `'.join(added)}`")
    if unknown:
        lines.append(f"â ï¸ Non trovati: `{'`, `'.join(unknown)}`")
    await update.message.reply_text("\n".join(lines) or "Nessun simbolo modificato.", parse_mode="Markdown")


async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("*Uso:* `/unmute BTCUSDT`", parse_mode="Markdown")
        return

    symbols = [s.upper() if s.upper().endswith("USDT") else s.upper() + "USDT" for s in context.args]
    removed = wm.unmute_symbols(symbols)
    lines   = []
    if removed:
        lines.append(f"ð Riattivati: `{'`, `'.join(removed)}`")
    else:
        lines.append("â ï¸ Nessuno di questi simboli era silenziato.")
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
            if s in muted:                    badges.append("ð")
            if alert_state != "none":         badges.append(f"ð´{alert_state.upper()}")
            if s in custom:                   badges.append("âï¸")
            badge_str = "  " + " ".join(badges) if badges else ""
            wl_lines.append(f"  â¢ `{s}`{badge_str}")
        wl_section = "\n".join(wl_lines)
    else:
        wl_section = "  _(tutti i simboli Bybit â nessun filtro)_"

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
            custom_lines.append(f"  `{sym}` â {', '.join(parts)}")
        custom_section = "\n".join(custom_lines)
    else:
        custom_section = "  _(usa soglie globali per tutti)_"

    text = (
        f"ð¯ *WATCHLIST â ModalitÃ : {mode}*\n\n"
        f"ð¡ *Simboli monitorati:*\n{wl_section}\n\n"
        f"ð *Silenziati:*\n{muted_section}\n\n"
        f"âï¸ *Soglie custom:*\n{custom_section}\n\n"
        f"_Usa /watch, /unwatch, /mute, /unmute, /alerts_"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# /alerts â Gestione soglie custom per simbolo
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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
      /alerts                          â mostra tutte le soglie custom
      /alerts BTCUSDT                  â mostra soglie per il simbolo
      /alerts BTCUSDT high 1.5         â imposta HIGH a 1.5% per BTC
      /alerts BTCUSDT reset            â riporta BTC ai default globali
    """
    args = context.args

    # Nessun argomento: mostra riepilogo globale
    if not args:
        custom = wm.get_all_custom_thresholds()
        if not custom:
            await update.message.reply_text(
                "â¹ï¸ *Nessuna soglia custom impostata.*\n\n"
                "Tutti i simboli usano le soglie globali:\n"
                "  ð´ HARD: 2.00%\n"
                "  ð¥ EXTREME: 1.50%\n"
                "  ð¨ HIGH: 1.00%\n"
                "  â¹ï¸ CHIUSURA: 0.23%\n"
                "  â RIENTRO: 0.75%\n\n"
                "*Uso:* `/alerts BTCUSDT high 1.5`\n"
                "*Reset:* `/alerts BTCUSDT reset`",
                parse_mode="Markdown",
            )
            return

        lines = ["âï¸ *SOGLIE CUSTOM ATTIVE*\n"]
        for sym, levels in sorted(custom.items()):
            lines.append(f"*{sym}*")
            for lvl, val in sorted(levels.items()):
                default = {"hard": 2.0, "extreme": 1.5, "high": 1.0, "close_tip": 0.23, "rientro": 0.75}.get(lvl, 0)
                arrow = "â" if val > default else "â"
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
            f"â Soglie di `{symbol}` ripristinate ai valori globali.",
            parse_mode="Markdown",
        )
        return

    # /alerts BTCUSDT  (mostra soglie del simbolo)
    if len(args) == 1:
        custom = wm.get_all_custom_thresholds().get(symbol, {})
        defaults = {"hard": 2.0, "extreme": 1.5, "high": 1.0, "close_tip": 0.23, "rientro": 0.75}
        lines = [f"âï¸ *Soglie per {symbol}*\n"]
        for lvl, default in defaults.items():
            val = custom.get(lvl, default)
            tag = " _âï¸ custom_" if lvl in custom else " _default_"
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
                f"â Valore non valido: `{args[2]}`\nUsa un numero (es. 1.5)",
                parse_mode="Markdown",
            )
            return

        if value <= 0 or value > 10:
            await update.message.reply_text(
                "â Il valore deve essere tra 0 e 10.",
                parse_mode="Markdown",
            )
            return

        ok = wm.set_custom_threshold(symbol, level, value)
        if not ok:
            levels_str = ", ".join(f"`{l}`" for l in _LEVEL_NAMES)
            await update.message.reply_text(
                f"â Livello `{level}` non valido.\nLivelli disponibili: {levels_str}",
                parse_mode="Markdown",
            )
            return

        default = {"hard": 2.0, "extreme": 1.5, "high": 1.0, "close_tip": 0.23, "rientro": 0.75}.get(level, 0)
        arrow = "â piÃ¹ restrittivo" if value > default else "â piÃ¹ sensibile"
        await update.message.reply_text(
            f"â Soglia custom impostata:\n"
            f"  `{symbol}` â {level}: `{value}%` ({arrow}, default: {default}%)",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        "*Uso:*\n"
        "`/alerts` â riepilogo globale\n"
        "`/alerts BTCUSDT` â soglie del simbolo\n"
        "`/alerts BTCUSDT high 1.5` â imposta soglia\n"
        "`/alerts BTCUSDT reset` â ripristina default",
        parse_mode="Markdown",
    )


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# Helper privati
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# /top10 â Classifica unificata in tempo reale
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

# Numero di simboli per lato (SHORT / LONG)
_TOP_N = 10

# Barra proporzionale (max 12 â)
_BAR_MAX = 12


def _rate_bar(rate_pct: float, max_abs: float) -> str:
    """Genera una barra â proporzionale al rate rispetto al massimo della lista."""
    if max_abs == 0:
        return "â"
    length = max(1, int(abs(rate_pct) / max_abs * _BAR_MAX))
    return "â" * length


def _level_badge(abs_rate: float) -> str:
    """Restituisce il badge di livello in base alle soglie fisse."""
    if abs_rate >= 2.00:
        return "ð´HARD"
    if abs_rate >= 1.50:
        return "ð¥EXT"
    if abs_rate >= 1.00:
        return "ð¨HIGH"
    if abs_rate >= 0.23:
        return "â¹ï¸CHI"
    return "âOK"


def _settlement_label(next_ts_ms: int) -> str:
    """Restituisce il tempo mancante al prossimo settlement in formato leggibile."""
    if not next_ts_ms:
        return "â"
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
    /top10 â Classifica dei 10 simboli con funding rate piÃ¹ estremi
    per lato SHORT (positivi) e LONG (negativi), in tempo reale.
    """
    msg = await update.message.reply_text("â³ Recupero dati in tempo reale...")

    tickers = await bc.get_funding_tickers()
    if not tickers:
        await msg.edit_text("â Impossibile recuperare i dati da Bybit. Riprova.")
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

    # Top 10 SHORT (rate piÃ¹ positivi)
    shorts = sorted(parsed, key=lambda x: x["rate"], reverse=True)[:_TOP_N]
    # Top 10 LONG  (rate piÃ¹ negativi)
    longs  = sorted(parsed, key=lambda x: x["rate"])[:_TOP_N]

    max_short = abs(shorts[0]["rate"]) if shorts else 1
    max_long  = abs(longs[0]["rate"])  if longs  else 1

    now_dt = datetime.now(TZ_IT).strftime("%H:%M %Z")

    # ââ Sezione SHORT âââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    short_lines = [
        f"â¡ *TOP {_TOP_N} SHORT* (funding positivo)",
        f"_Aggiornato: {now_dt}_",
        "`#   Simbolo        Rate      Lvl   Next  24H`",
        "`âââââââââââââââââââââââââââââââââââââââââââââ`",
    ]
    for i, t in enumerate(shorts, 1):
        bar      = _rate_bar(t["rate"], max_short)
        badge    = _level_badge(t["rate"])
        settle   = _settlement_label(t["next_ts"])
        p24h     = f"{t['pct_24h']:+.1f}%"
        interval = f"{t['interval_h']}H"
        short_lines.append(
            f"`{i:>2}.` *{t['symbol']:<12}* `{t['rate']:+.4f}%`\n"
            f"     `{bar:<12}` {badge} Â· {interval} Â· {settle} Â· {p24h}"
        )

    # ââ Sezione LONG ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    long_lines = [
        "",
        f"â¡ *TOP {_TOP_N} LONG* (funding negativo)",
        "`#   Simbolo        Rate      Lvl   Next  24H`",
        "`âââââââââââââââââââââââââââââââââââââââââââââ`",
    ]
    for i, t in enumerate(longs, 1):
        bar      = _rate_bar(t["rate"], max_long)
        badge    = _level_badge(abs(t["rate"]))
        settle   = _settlement_label(t["next_ts"])
        p24h     = f"{t['pct_24h']:+.1f}%"
        interval = f"{t['interval_h']}H"
        long_lines.append(
            f"`{i:>2}.` *{t['symbol']:<12}* `{t['rate']:+.4f}%`\n"
            f"     `{bar:<12}` {badge} Â· {interval} Â· {settle} Â· {p24h}"
        )

    # ââ Footer statistiche ââââââââââââââââââââââââââââââââââââââââââââââââââââ
    total_sym   = len(parsed)
    extreme_sym = sum(1 for t in parsed if abs(t["rate"]) >= 1.0)
    hard_sym    = sum(1 for t in parsed if abs(t["rate"]) >= 2.0)
    avg_abs     = sum(abs(t["rate"]) for t in parsed) / total_sym if total_sym else 0

    footer = [
        "",
        "âââââââââââââââââââââââââââââ",
        f"ð *Mercato* â {total_sym} simboli monitorati",
        f"   ð¨ â¥1%: {extreme_sym}   ð´ â¥2%: {hard_sym}   Media: {avg_abs:.4f}%",
    ]

    # ââ Invio ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    full_msg = "\n".join(short_lines + long_lines + footer)

    # Telegram: max 4096 char â se supera split in 2
    if len(full_msg) > 4000:
        part1 = "\n".join(short_lines + footer)
        part2 = "\n".join(long_lines[1:] + footer)  # [1:] salta riga vuota iniziale
        await msg.edit_text(part1, parse_mode="Markdown")
        await update.message.reply_text(part2, parse_mode="Markdown")
    else:
        await msg.edit_text(full_msg, parse_mode="Markdown")


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# BACKTEST
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

async def backtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /backtest <SYMBOL>         â Report completo su un simbolo (30gg)
    /backtest top10            â Classifica top 10 simboli piÃ¹ volatili
    /backtest watchlist        â Analizza tutti i simboli nella watchlist

    Esempi:
      /backtest SOLUSDT
      /backtest top10
      /backtest watchlist
    """
    args = context.args or []

    if not args:
        await update.message.reply_text(
            "ð *BACKTEST â Uso corretto:*\n"
            "âââââââââââââââââââââ\n"
            "`/backtest SOLUSDT`       â Singolo simbolo\n"
            "`/backtest top10`         â Top 10 piÃ¹ volatili\n"
            "`/backtest watchlist`     â Tua watchlist\n\n"
            "_Simula profitti/perdite basati sugli alert del bot negli ultimi 30 giorni._\n"
            "_Include fee taker (0.055%) + slippage (0.02%) per lato._",
            parse_mode="Markdown",
        )
        return

    subcmd = args[0].upper()

    # ââ /backtest top10 âââââââââââââââââââââââââââââââââââââââââââââââââââ
    if subcmd == "TOP10":
        wait_msg = await update.message.reply_text(
            "â³ *Backtest top 10 simboliâ¦*\n"
            "_Recupero dati 30gg da Bybit (puÃ² richiedere 30-60 secondi)_",
            parse_mode="Markdown",
        )
        try:
            # Prendi i 10 simboli con funding rate assoluto piÃ¹ alto
            tickers = await bc.get_funding_tickers()
            if not tickers:
                await wait_msg.edit_text("â Impossibile recuperare i ticker da Bybit.")
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
            await wait_msg.edit_text(f"â Errore durante il backtest: {exc}")
        return

    # ââ /backtest watchlist âââââââââââââââââââââââââââââââââââââââââââââââ
    if subcmd == "WATCHLIST":
        symbols = list(wm.get_watchlist())
        if not symbols:
            await update.message.reply_text(
                "â ï¸ La tua watchlist Ã¨ vuota.\n"
                "Aggiungi simboli con `/watch BTCUSDT SOLUSDT`",
                parse_mode="Markdown",
            )
            return

        wait_msg = await update.message.reply_text(
            f"â³ *Backtest watchlist ({len(symbols)} simboli)â¦*\n"
            f"_Recupero dati 30gg da Bybitâ¦_",
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
            await wait_msg.edit_text(f"â Errore durante il backtest: {exc}")
        return

    # ââ /backtest SYMBOL ââââââââââââââââââââââââââââââââââââââââââââââââââ
    symbol = subcmd
    # Normalizza (aggiunge USDT se non presente)
    if not symbol.endswith("USDT") and not symbol.endswith("USDC"):
        symbol = symbol + "USDT"

    wait_msg = await update.message.reply_text(
        f"â³ *Backtest {symbol}â¦*\n"
        f"_Recupero {bt.DAYS_BACK} giorni di funding rate da Bybitâ¦_",
        parse_mode="Markdown",
    )

    try:
        entries = await bt.fetch_30d(symbol)
        if not entries:
            await wait_msg.edit_text(
                f"â Nessun dato trovato per `{symbol}`.\n"
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
        await wait_msg.edit_text(f"â Errore durante il backtest di {symbol}: {exc}")


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# Registrazione handlers
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# /profitto_funding
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

async def profitto_funding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Mostra il riepilogo dei guadagni da funding per le posizioni aperte.

    Per ogni simbolo che ha ricevuto alert HIGH/EXTREME/HARD e aveva una
    posizione aperta, mostra:
      - Rate dell'ultimo ciclo di funding
      - Guadagno/costo dell'ultimo ciclo
      - Totale guadagno/costo da quando la posizione Ã¨ aperta
    """
    if not _has_credentials(update.effective_chat.id):
        await update.message.reply_text("â ï¸ Configura prima le tue API Key con /start")
        return

    await update.message.reply_text("ð¹ Recupero guadagni funding...")

    # Recupera posizioni aperte per arricchire il riepilogo
    try:
        positions = await bc.get_positions()
    except Exception:
        positions = []

    text = ft.format_summary(positions if positions else None)
    await update.message.reply_text(text, parse_mode="Markdown")




# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# /rischio â Analisi rischio posizioni aperte
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
async def rischio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Analisi del rischio per ogni posizione aperta: distanza liquidazione, leverage, PnL%."""
    if not _has_credentials(update.effective_chat.id):
        await update.message.reply_text("â ï¸ Configura prima le tue API Key con /start")
        return
    await update.message.reply_text("â ï¸ Analisi rischio in corso...")
    try:
        positions = await bc.get_positions()
    except Exception as e:
        await update.message.reply_text(f"â Errore: {e}")
        return
    if not positions:
        await update.message.reply_text("ð­ Nessuna posizione aperta.")
        return

    lines = ["â ï¸ *ANALISI RISCHIO POSIZIONI*", ""]
    for p in positions:
        sym        = p.get("symbol", "")
        side_raw   = p.get("side", "Buy")
        side       = "ð¢ LONG" if side_raw == "Buy" else "ð´ SHORT"
        mark       = float(p.get("markPrice", 0))
        liq        = float(p.get("liqPrice", 0) or 0)
        lev        = float(p.get("leverage", 1) or 1)
        upnl       = float(p.get("unrealisedPnl", 0))
        pnl_pct    = float(p.get("unrealisedPnlPcnt", 0))
        size       = float(p.get("size", 0))
        pos_val    = float(p.get("positionValue", 0))

        if liq > 0 and mark > 0:
            if side_raw == "Buy":
                dist_pct = (mark - liq) / mark * 100
            else:
                dist_pct = (liq - mark) / mark * 100
            dist_pct = max(dist_pct, 0)
            if dist_pct < 5:
                risk_emoji = "ð´ CRITICO"
            elif dist_pct < 10:
                risk_emoji = "ð  ALTO"
            elif dist_pct < 20:
                risk_emoji = "ð¡ MEDIO"
            else:
                risk_emoji = "ð¢ BASSO"
            dist_str = f"{dist_pct:.1f}% ({risk_emoji})"
        else:
            dist_str = "N/D"

        sign = "+" if upnl >= 0 else ""
        lines.append(f"*{sym}* {side} {lev:.0f}x")
        lines.append(f"  Mark: `{mark:.4f}` | Liq: `{liq:.4f}`")
        lines.append(f"  Distanza liq: `{dist_str}`")
        lines.append(f"  PnL: `{sign}{upnl:.2f} USDT` ({sign}{pnl_pct:.2f}%)")
        lines.append(f"  Valore pos: `{pos_val:.2f} USDT`")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# /summary â Riepilogo rapido portafoglio
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Riepilogo rapido: wallet + posizioni aperte."""
    if not _has_credentials(update.effective_chat.id):
        await update.message.reply_text("â ï¸ Configura prima le tue API Key con /start")
        return
    await update.message.reply_text("ð Calcolo summary...")
    try:
        wallet    = await bc.get_wallet()
        positions = await bc.get_positions()
    except Exception as e:
        await update.message.reply_text(f"â Errore: {e}")
        return

    equity  = wallet.get("equity", 0)
    upnl    = wallet.get("upnl", 0)
    rpnl    = wallet.get("realisedPnl", 0)
    avail   = wallet.get("avail", 0)
    margin  = wallet.get("margin", 0)

    n_long  = sum(1 for p in positions if p.get("side") == "Buy")
    n_short = sum(1 for p in positions if p.get("side") == "Sell")
    tot_upnl = sum(float(p.get("unrealisedPnl", 0)) for p in positions)

    best_sym = max(positions, key=lambda p: float(p.get("unrealisedPnl", 0)), default=None)
    worst_sym = min(positions, key=lambda p: float(p.get("unrealisedPnl", 0)), default=None)

    now_it = datetime.now(TZ_IT).strftime("%d/%m/%Y %H:%M")
    lines = [
        f"ð *SUMMARY PORTAFOGLIO â {now_it}*", "",
        f"ð¼ Equity: `{equity:.2f} USDT`",
        f"ðµ Disponibile: `{avail:.2f} USDT`",
        f"ð Unrealised PnL: `{upnl:+.2f} USDT`",
        f"ð° Realised PnL: `{rpnl:+.2f} USDT`",
        f"ð Margine usato: `{margin:.2f} USDT`",
        "",
        f"ð Posizioni: `{len(positions)}` (ð¢ {n_long} LONG | ð´ {n_short} SHORT)",
        f"ð PnL totale aperte: `{tot_upnl:+.2f} USDT`",
    ]
    if best_sym:
        b_pnl = float(best_sym.get("unrealisedPnl", 0))
        lines.append(f"ð Migliore: {best_sym.get('symbol')} `{b_pnl:+.2f} USDT`")
    if worst_sym:
        w_pnl = float(worst_sym.get("unrealisedPnl", 0))
        lines.append(f"ð Peggiore: {worst_sym.get('symbol')} `{w_pnl:+.2f} USDT`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# /newlistings â Nuovi listing con funding elevato
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
async def newlistings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra nuovi listing (ultimi 30gg) con funding rate elevato."""
    await update.message.reply_text("ð Recupero nuovi listing...")
    try:
        tickers = await bc.get_funding_tickers()
        # Ordina per funding rate assoluto decrescente e prendi i top 20
        items = sorted(tickers, key=lambda t: abs(float(t.get("fundingRate", 0))), reverse=True)[:20]
        items = [{"symbol": t["symbol"], "fundingRate": float(t.get("fundingRate",0))*100,
                  "markPrice": t.get("lastPrice", 0), "price24hPcnt": float(t.get("price24hPcnt",0))*100,
                  "daysAgo": 0} for t in items]
    except Exception as e:
        await update.message.reply_text(f"â Errore: {e}")
        return

    if not items:
        await update.message.reply_text("ð­ Nessun nuovo listing trovato.")
        return

    # Filtra per funding rate notevole o mostra tutti
    notable = [i for i in items if abs(float(i.get("fundingRate", 0))) >= 0.5]
    show = notable if notable else items[:10]

    lines = [f"ð *NUOVI LISTING ({len(items)} totali, ultimi 30gg)*", ""]
    for item in show[:15]:
        sym  = item.get("symbol", "")
        fr   = float(item.get("fundingRate", 0))
        days = float(item.get("daysAgo", 0))
        mp   = float(item.get("markPrice", 0))
        pct  = float(item.get("price24hPcnt", 0))
        sign = "+" if fr >= 0 else ""
        fr_badge = "ð¥" if abs(fr) >= 2.0 else "â¡" if abs(fr) >= 1.0 else "ð"
        lines.append(
            f"{fr_badge} *{sym}* â {days:.0f}gg fa\n"
            f"  FR: `{sign}{fr:.4f}%` | Price: `{mp:.4f}` ({pct:+.2f}%)"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# /analytics â Metriche avanzate funding
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
async def analytics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra posizioni aperte e storico alert recenti."""
    await update.message.reply_text("📊 Carico dati...")
    try:
        # ── Posizioni aperte ──────────────────────────────────────────────
        pos_data = await bc.get_positions()
        positions = pos_data if isinstance(pos_data, list) else []

        if positions:
            pos_lines = []
            total_upnl = 0.0
            for p in positions:
                sym   = p.get("symbol", "?")
                side  = p.get("side", "?")
                size  = p.get("size", 0)
                upnl  = float(p.get("unrealisedPnl", 0))
                pct   = float(p.get("unrealisedPnlPcnt", 0)) * 100
                total_upnl += upnl
                icon  = "🟢" if upnl >= 0 else "🔴"
                pos_lines.append(
                    f"  {icon} {sym} {side} {size} | uPnL: {upnl:+.2f}$ ({pct:+.2f}%)"
                )
            pos_text = "\n".join(pos_lines)
            upnl_icon = "🟢" if total_upnl >= 0 else "🔴"
            pos_section = (
                f"📂 *Posizioni aperte ({len(positions)})*\n"
                f"{pos_text}\n"
                f"  {upnl_icon} uPnL totale: {total_upnl:+.2f}$"
            )
        else:
            pos_section = "📂 *Posizioni aperte*\n  Nessuna posizione aperta"

        # ── Alert recenti ─────────────────────────────────────────────────
        active_alerts = al.get_all_states()
        if active_alerts:
            alert_lines = [
                f"  • {sym} — {d['level'].upper()}"
                for sym, d in list(active_alerts.items())[:15]
            ]
            alert_section = "📡 *Alert attivi ora*\n" + "\n".join(alert_lines)
        else:
            alert_section = "📡 *Alert attivi ora*\n  Nessuno"

        text = (
            "📊 *ANALYTICS — Posizioni & Alert*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"{pos_section}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"{alert_section}\n\n"
            "🔔 _Modalità: ALERT ONLY — nessun trading attivo_"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"❌ Errore: {e}")


async def alert_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra e permette di configurare le soglie di alert."""
    import alert_logic as _al

    lines = [
        "âï¸ *CONFIGURAZIONE SOGLIE ALERT*", "",
        "*Soglie globali:*",
        f"  ð JACKPOT:  `> {_al.THR_JACKPOT:.2f}%`",
        f"  ð¥ EXTREME:  `> {_al.THR_EXTREME:.2f}%`",
        f"  â¡ HARD:     `> {_al.THR_HARD:.2f}%`",
        f"  ð HIGH:     `> {_al.THR_HIGH:.2f}%`",
        f"  â¬ï¸ CLOSE_TIP: `> {_al.THR_CLOSE_TIP:.2f}%`",
        f"  â¬ï¸ RIENTRO:  `< {_al.RESET_THRESHOLD:.2f}%`",
        "",
        "Per modificare le soglie usa i parametri nel file .env:",
        "`THR_JACKPOT`, `THR_EXTREME`, `THR_HARD`, `THR_HIGH`",
        "",
        "*Alert liquidazione imminente:*",
        "  ð´ Attivo quando distanza < 15% dal prezzo di liq.",
        "",
        "*Per aggiungere soglie custom per simbolo:*",
        "  `/alerts BTCUSDT` â mostra soglie correnti",
    ]

    try:
        lines.append("")
        lines.append("*Simboli con soglie custom:*")
        custom = _al.get_custom_thresholds() if hasattr(_al, 'get_custom_thresholds') else {}
        if custom:
            for sym, thr in list(custom.items())[:10]:
                lines.append(f"  â¢ {sym}: `{thr:.2f}%`")
        else:
            lines.append("  Nessuna soglia custom impostata")
    except:
        pass

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")




# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# PARAMETRI DI RISCHIO â configurabili da /settings
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
_risk_params = {
    "max_leverage":       10.0,   # leva massima consentita per trade
    "max_positions":      10,     # numero massimo posizioni simultanee
    "max_pct_per_trade":  5.0,    # % massima del capitale per singolo trade
}
def get_risk_params() -> dict:
    """Restituisce i parametri di rischio correnti."""
    return dict(_risk_params)
async def deletekeys_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Elimina le credenziali Bybit dell'utente dal bot."""
    chat_id = update.effective_chat.id
    if user_store.delete(chat_id):
        session_manager.remove_session(chat_id)
        await update.message.reply_text(
            "ðï¸ *Credenziali eliminate.*\n\n"
            "Le tue API Key e Secret sono state rimosse.\n"
            "Usa /start per configurarne di nuove.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "â¹ï¸ Nessuna credenziale trovata per questo account.",
        )


def register(app):
    """Registra tutti i command handler sull'applicazione Telegram."""

    # Setup wizard (ConversationHandler)
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MENU: [CallbackQueryHandler(menu_callback)],
            WAITING_API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_key)],
            WAITING_API_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_secret)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)

    # Comandi semplici
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("test", test_cmd))
    app.add_handler(CommandHandler("top10", top10))
    app.add_handler(CommandHandler("funding_top", funding_top))
    app.add_handler(CommandHandler("funding_bottom", funding_bottom))
    app.add_handler(CommandHandler("storico", storico))
    app.add_handler(CommandHandler("storico7g", storico7g))
    app.add_handler(CommandHandler("saldo", saldo))
    app.add_handler(CommandHandler("posizioni", posizioni))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("unwatch", unwatch_cmd))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CommandHandler("watchlist", watchlist_cmd))
    app.add_handler(CommandHandler("alerts", alerts_cmd))
    app.add_handler(CommandHandler("backtest", backtest_cmd))
    app.add_handler(CommandHandler("profitto_funding", profitto_funding))
    app.add_handler(CommandHandler("rischio",      rischio))
    app.add_handler(CommandHandler("summary",      summary))
    app.add_handler(CommandHandler("newlistings",  newlistings))
    app.add_handler(CommandHandler("analytics",    analytics_cmd))
    app.add_handler(CommandHandler("alert_config", alert_config))
    app.add_handler(CommandHandler("deletekeys",   deletekeys_cmd))
