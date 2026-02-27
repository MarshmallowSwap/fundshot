"""
commands.py — Funding King Bot
Tutti i command handler Telegram + setup wizard.
"""

import os
import logging
import re
from datetime import datetime, timezone

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

logger = logging.getLogger(__name__)

# ── ConversationHandler states ────────────────────────────────────────────────
MENU, WAITING_API_KEY, WAITING_API_SECRET = range(3)


def is_watched(symbol: str) -> bool:
    """Proxy verso watchlist_manager — usato da bot.py."""
    return wm.is_watched(symbol)


# ══════════════════════════════════════════════════════════════════════════════
# /start — Setup Wizard
# ══════════════════════════════════════════════════════════════════════════════

def _has_credentials() -> bool:
    return bool(os.getenv("BYBIT_API_KEY")) and bool(os.getenv("BYBIT_API_SECRET"))


def _build_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Imposta API Key", callback_data="set_api_key")],
        [InlineKeyboardButton("🔒 Imposta API Secret", callback_data="set_api_secret")],
        [InlineKeyboardButton("✅ Conferma e Avvia", callback_data="confirm_start")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id

    # Salva chat_id automaticamente
    if not os.getenv("CHAT_ID"):
        _set_env("CHAT_ID", str(chat_id))

    if _has_credentials():
        await update.message.reply_text(
            "🤖 *Funding King Bot* — Attivo ✅\n\n"
            f"Chat ID: `{chat_id}`\n"
            f"API Key: `{_mask(os.getenv('BYBIT_API_KEY', ''))}`\n\n"
            "Usa /help per vedere tutti i comandi.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    key = _mask(os.getenv("BYBIT_API_KEY", ""))
    secret = _mask(os.getenv("BYBIT_API_SECRET", ""))
    text = (
        "🤖 *Funding King Bot — Setup*\n\n"
        f"Chat ID: `{chat_id}` ✅ (rilevato automaticamente)\n"
        f"API Key: `{key or '⚠️ non impostata'}`\n"
        f"API Secret: `{secret or '⚠️ non impostato'}`\n\n"
        "Seleziona cosa configurare:"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=_build_menu_keyboard())
    return MENU


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
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
        if not _has_credentials():
            await query.edit_message_text(
                "⚠️ Configura prima API Key e API Secret.",
                reply_markup=_build_menu_keyboard(),
            )
            return MENU
        bc.reload_session()
        await query.edit_message_text(
            "✅ *Configurazione completata!*\n\n"
            f"API Key: `{_mask(os.getenv('BYBIT_API_KEY', ''))}`\n"
            f"API Secret: `{_mask(os.getenv('BYBIT_API_SECRET', ''))}`\n\n"
            "Il bot inizia il monitoraggio. Usa /help per i comandi.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    return MENU


async def receive_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    _set_env("BYBIT_API_KEY", value)
    bc.reload_session()
    await update.message.reply_text(
        f"✅ API Key salvata: `{_mask(value)}`",
        parse_mode="Markdown",
        reply_markup=_build_menu_keyboard(),
    )
    return MENU


async def receive_api_secret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    _set_env("BYBIT_API_SECRET", value)
    bc.reload_session()
    await update.message.reply_text(
        f"✅ API Secret salvato: `{_mask(value)}`",
        parse_mode="Markdown",
        reply_markup=_build_menu_keyboard(),
    )
    return MENU


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Setup annullato. Usa /start per ricominciare.")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# /help
# ══════════════════════════════════════════════════════════════════════════════

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 *FUNDING KING BOT — Comandi disponibili*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 *FUNDING RATE*\n"
        "/funding\\_top — Top 10 funding positivi (SHORT)\n"
        "/funding\\_bottom — Top 10 funding negativi (LONG)\n"
        "/top10 — Classifica 10 SHORT + 10 LONG in tempo reale\n"
        "/storico `<SIMBOLO>` — Ultimi 8 cicli\n"
        "/storico7g `<SIMBOLO>` — Storico 7 giorni con grafici\n"
        "/backtest `<SYM|top10|watchlist>` — Simula P&L 30 giorni\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "💼 *ACCOUNT*\n"
        "/saldo — Saldo wallet Bybit\n"
        "/posizioni — Posizioni aperte con PnL%\n\n"
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
        "/help — Questo messaggio\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🔔 *Alert automatici ogni 60s:*\n"
        "🔴 HARD ≥ ±2% | 🔥 EXTREME ≥ ±1.5%\n"
        "🚨 HIGH ≥ ±1% | ℹ️ CHIUSURA ≥ ±0.23%\n"
        "✅ RIENTRO ≤ ±0.75% | ⏰ Prossimo funding\n"
        "🚀 PUMP/💥 DUMP ≥ ±5% in 1H\n"
        "💧 Liquidazioni ≥ $100k"
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
        delta = datetime.now(timezone.utc) - uptime
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

    all_ok = p.get("ok") and a.get("ok") and pos.get("ok")
    summary = "✅ Tutti i test superati" if all_ok else "⚠️ Alcuni test falliti"

    text = (
        f"🔧 *TEST CONNESSIONE BYBIT*\n\n"
        f"1️⃣ API Pubblica\n   {pub_line}\n\n"
        f"2️⃣ API Autenticata\n   {auth_line}\n\n"
        f"3️⃣ Posizioni\n   {pos_line}\n\n"
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
    args = context.args
    if not args:
        await update.message.reply_text("Uso: /storico BTCUSDT")
        return
    symbol = args[0].upper()
    await update.message.reply_text(f"📊 Storico funding {symbol}...")

    history = await bc.get_funding_history(symbol, limit=8)
    if not history:
        await update.message.reply_text(f"Nessun dato per {symbol}.")
        return

    lines = [f"📅 *STORICO FUNDING — {symbol}*\n"]
    for entry in history:
        rate = float(entry.get("fundingRate", 0)) * 100
        ts = int(entry.get("fundingRateTimestamp", 0)) // 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d/%m %H:%M")
        emoji = "🟢" if rate >= 0 else "🔴"
        lines.append(f"{emoji} {dt} UTC → *{rate:+.4f}%*")

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


async def storico7g(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "Uso: `/storico7g BTCUSDT`\nMostra storico funding ultimi 7 giorni con statistiche.",
            parse_mode="Markdown",
        )
        return

    symbol = args[0].upper()
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
    max_dt    = datetime.fromtimestamp(timestamps[max_idx], tz=timezone.utc).strftime("%d/%m %H:%M")
    min_dt    = datetime.fromtimestamp(timestamps[min_idx], tz=timezone.utc).strftime("%d/%m %H:%M")
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
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d/%m")
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
        f"  Max:  `{max_rate:+.4f}%`  ({max_dt} UTC)",
        f"  Min:  `{min_rate:+.4f}%`  ({min_dt} UTC)",
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
    if not _has_credentials():
        await update.message.reply_text("⚠️ Configura le credenziali con /start.")
        return
    await update.message.reply_text("💼 Recupero saldo...")

    wallet = await bc.get_wallet_balance()
    if not wallet:
        await update.message.reply_text("❌ Impossibile recuperare il saldo. Controlla le API key con /test.")
        return

    pnl_emoji = "✅" if wallet["totalPerpUPL"] >= 0 else "❌"
    lines = [
        "💼 *SALDO ACCOUNT — Bybit*\n",
        f"Equity totale:      `${wallet['totalEquity']:>12,.2f}`",
        f"Wallet balance:     `${wallet['totalWalletBalance']:>12,.2f}`",
        f"Margine disponibile:`${wallet['totalAvailableBalance']:>12,.2f}`",
        f"Margine impegnato:  `${wallet['totalInitialMargin']:>12,.2f}`",
        f"PnL aperto:         `${wallet['totalPerpUPL']:>+12,.2f}` {pnl_emoji}",
        "",
        "🪙 *Saldi per coin:*",
    ]
    for c in wallet["coins"]:
        lines.append(f"  {c['coin']}: `{c['walletBalance']:,.4f}` (≈ ${c['usdValue']:,.2f})")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# /posizioni
# ══════════════════════════════════════════════════════════════════════════════

async def posizioni(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _has_credentials():
        await update.message.reply_text("⚠️ Configura le credenziali con /start.")
        return
    await update.message.reply_text("📋 Recupero posizioni...")

    positions = await bc.get_positions()
    if not positions:
        await update.message.reply_text("📭 Nessuna posizione aperta.")
        return

    lines = ["📋 *POSIZIONI APERTE — Bybit*\n"]
    total_pnl = 0.0

    for i, p in enumerate(positions, 1):
        side_emoji = "🟢" if p["side"] == "Buy" else "🔴"
        direction = "LONG" if p["side"] == "Buy" else "SHORT"
        pnl = p["unrealisedPnl"]
        pnl_pct = p["pnlPct"]
        total_pnl += pnl
        pnl_emoji = "✅" if pnl >= 0 else "❌"
        status = "⚠️ Liquidazione!" if p["positionStatus"] == "Liq" else ""

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

    total_emoji = "✅" if total_pnl >= 0 else "❌"
    lines.append(f"─────────────────────")
    lines.append(f"Totale PnL aperto: `{total_pnl:+,.2f} $` {total_emoji}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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
                "ℹ️ *Nessuna soglia custom impostata.*\n\n"
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

    now_dt = datetime.now(timezone.utc).strftime("%H:%M UTC")

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
        short_lines.append(
            f"`{i:>2}.` *{t['symbol']:<12}* `{t['rate']:+.4f}%`\n"
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
        long_lines.append(
            f"`{i:>2}.` *{t['symbol']:<12}* `{t['rate']:+.4f}%`\n"
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
