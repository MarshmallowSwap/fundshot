"""
bot.py — Funding King Bot
Entry point principale: avvio bot Telegram, job di monitoraggio, WebSocket liquidazioni.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

TZ_IT = ZoneInfo("Europe/Rome")

from dotenv import load_dotenv
from telegram import Bot
from telegram.ext import ApplicationBuilder

import bybit_client as bc
import alert_logic as al
try:
    import alert_config_manager as _acm
    _ACM_AVAILABLE = True
except ImportError:
    _acm = None
    _ACM_AVAILABLE = False

def _bot_alert_enabled(alert_type: str) -> bool:
    if _ACM_AVAILABLE and _acm:
        return _acm.is_enabled(alert_type)
    return True

import commands
import user_store as _user_store
import ws_liquidations as wsl
import watchlist_manager as wm
import funding_tracker as ft

# ── Configurazione ─────────────────────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID      = os.getenv("CHAT_ID", "")
JOB_INTERVAL = int(os.getenv("JOB_INTERVAL", 60))


# ── Helper: invia messaggio Telegram ─────────────────────────────────────────
async def send_alert(bot: Bot, text: str, target_chat_id=None):
    """Invia alert a un utente specifico o a tutti gli utenti con credenziali."""
    if target_chat_id:
        recipients = [str(target_chat_id)]
    else:
        recipients = _user_store.users_with_credentials()
        if not recipients:
            fallback = os.getenv("CHAT_ID", CHAT_ID)
            if fallback:
                recipients = [fallback]
    for cid in recipients:
        try:
            await bot.send_message(chat_id=cid, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error("Errore invio alert a %s: %s", cid, e)



# ── Alert liquidazione imminente ─────────────────────────────────────────────
_LIQ_ALERT_SENT: dict[str, float] = {}   # symbol → last liq% when alert sent
LIQ_WARN_PCT = 15.0                       # alert se margine residuo < 15%

async def _check_liq_and_level(bot: Bot, symbol: str, mark_price: float, liq_price: float,
                                 side: str, rate_pct: float, bot_data: dict):
    """Invia alert Telegram se il prezzo si avvicina al prezzo di liquidazione."""
    if liq_price <= 0 or mark_price <= 0:
        return
    if side == "Buy":
        dist_pct = (mark_price - liq_price) / mark_price * 100
    else:
        dist_pct = (liq_price - mark_price) / mark_price * 100
    if dist_pct < 0:
        dist_pct = 0.0

    last_sent = _LIQ_ALERT_SENT.get(symbol, 9999)
    # Invia alert se distanza < 15% e non già inviato per questa soglia
    if dist_pct < LIQ_WARN_PCT and (last_sent - dist_pct) > 2.0:
        _LIQ_ALERT_SENT[symbol] = dist_pct
        side_label = "LONG" if side == "Buy" else "SHORT"
        emoji = "🔴" if dist_pct < 5 else "🟠"
        msg = (
            f"{emoji} *LIQUIDAZIONE IMMINENTE — {symbol}*\n"
            f"Side: {side_label} | Rate: {rate_pct:+.4f}%\n"
            f"Mark: `{mark_price:.4f}` | Liq: `{liq_price:.4f}`\n"
            f"Distanza dalla liquidazione: *{dist_pct:.1f}%*"
        )
        await send_alert(bot, msg)
        bot_data["alerts_sent"] = bot_data.get("alerts_sent", 0) + 1
    elif dist_pct >= LIQ_WARN_PCT and symbol in _LIQ_ALERT_SENT:
        # Reset soglia quando il pericolo si allontana
        del _LIQ_ALERT_SENT[symbol]

# ── Job principale: monitoraggio funding ──────────────────────────────────────
# Tasso precedente per simbolo: rilevare reset ciclo funding
_prev_rates: dict[str, float] = {}


_fj_running = False  # lock anti-sovrapposizione

async def funding_job(context):
    global _fj_running
    if _fj_running:
        logger.warning("⚠️ funding_job: job precedente ancora in esecuzione, skip")
        return
    _fj_running = True
    bot: Bot  = context.bot
    bot_data  = context.bot_data

    try:
        tickers = await bc.get_funding_tickers()
    except Exception as e:
        logger.error("funding_job: errore fetch tickers: %s", e)
        return

    bot_data["symbols_count"] = len(tickers)
    bot_data["monitoring"]    = True
    bot_data["last_cycle"]    = datetime.now(TZ_IT).strftime("%d/%m/%Y %H:%M:%S %Z")

    if not tickers:
        logger.warning("Nessun ticker ricevuto.")
        return


    # Pre-carica posizioni UNA SOLA VOLTA (evita N chiamate API nel loop)
    try:
        positions_all = await bc.get_positions()
    except Exception as _e_pos:
        logger.warning("funding_job: errore pre-fetch posizioni: %s", _e_pos)
        positions_all = []

    for ticker in tickers:
        symbol = ticker.get("symbol", "")
        if not commands.is_watched(symbol):
            continue

        rate_raw        = float(ticker.get("fundingRate", 0))
        rate_pct        = rate_raw * 100
        interval_h      = ticker.get("fundingIntervalHour", 8)
        next_funding_ts = int(ticker.get("nextFundingTime", 0))
        prev_price_1h   = ticker.get("prevPrice1h", "0")
        pct_24h         = ticker.get("price24hPcnt", "0")
        last_price      = ticker.get("lastPrice", "0")

        # ── Aggiorna storico rolling (per soglie dinamiche) ────────────────
        al.update_rate_history(symbol, rate_pct)

        # 1. Alert funding rate
        alert_text = al.process_funding(symbol, rate_pct, interval_h)
        if alert_text:
            await send_alert(bot, alert_text)
            bot_data["alerts_sent"] = bot_data.get("alerts_sent", 0) + 1


        # 1b. Alert cambio livello funding
        if _bot_alert_enabled("level_change"):
            level_alert = al.check_level_change(symbol, al.classify(symbol, rate_pct))
            if level_alert:
                await send_alert(bot, level_alert)
                bot_data["alerts_sent"] = bot_data.get("alerts_sent", 0) + 1

        # 2. Alert prossimo funding (entro X minuti)
        if next_funding_ts:
            next_text = al.process_next_funding(symbol, rate_pct, interval_h, next_funding_ts)
            if next_text:
                await send_alert(bot, next_text)
                bot_data["alerts_sent"] = bot_data.get("alerts_sent", 0) + 1

        # 3. Alert PUMP/DUMP — RIMOSSO (utente non desidera questi alert)

        # 4. Tracking guadagno funding: rileva reset ciclo (rate quasi zero)
        #    Logica: prev_rate era HIGH+ (>= 0.50%) e ora è quasi zero → funding pagato
        RESET_THR    = al.RESET_THRESHOLD          # default 0.02
        HIGH_THR     = 0.50                        # rate minimo per considerare il ciclo "rilevante"
        prev_rate    = _prev_rates.get(symbol, 0.0)
        _prev_rates[symbol] = rate_pct              # aggiorna sempre il tasso corrente

        if abs(rate_pct) <= RESET_THR and abs(prev_rate) >= HIGH_THR and al.is_funded(symbol):
            # Il ciclo è appena resettato: recupera posizioni e registra gain
            try:
                positions = await bc.get_positions()
                pos = next((p for p in positions if p["symbol"] == symbol), None)
                if pos:
                    size       = float(pos.get("size", 0))
                    mark_price = float(pos.get("markPrice", 0))
                    side       = pos.get("side", "Buy")
                    level      = al.classify(symbol, prev_rate) if abs(prev_rate) > 0 else "high"
                    # Se classify restituisce "none" usa il livello dello stato precedente
                    if level == "none":
                        level = "high"
                    if size > 0 and mark_price > 0:
                        gain = ft.record_cycle(
                            symbol=symbol,
                            rate_pct=prev_rate,
                            mark_price=mark_price,
                            size=size,
                            side=side,
                            level=level,
                        )
                        sign = "+" if gain >= 0 else ""
                        gain_dir = "ricevuto" if gain >= 0 else "pagato"
                        msg = (
                            f"💰 *FUNDING REGISTRATO — {symbol}*\n"
                            f"Rate ciclo: {'+' if prev_rate>=0 else ''}{prev_rate:.4f}%\n"
                            f"Posizione: {'SHORT' if side=='Sell' else 'LONG'} {size}\n"
                            f"Gain {gain_dir}: `{sign}{gain:.4f} USDT`"
                        )
                        await send_alert(bot, msg)
            except Exception as e:
                logger.warning("funding_tracker: errore calcolo gain %s: %s", symbol, e)

        # 5. Alert liquidazione imminente
        try:
            positions_liq = positions_all  # pre-caricato fuori dal loop
            pos_liq = next((p for p in positions_liq if p.get("symbol") == symbol), None)
            if pos_liq and float(pos_liq.get("size", 0)) > 0:
                if _bot_alert_enabled("liquidation"):
                    await _check_liq_and_level(
                        bot, symbol,
                        float(pos_liq.get("markPrice", 0)),
                        float(pos_liq.get("liqPrice", 0) or 0),
                        pos_liq.get("side", "Buy"),
                        rate_pct, bot_data
                    )
        except Exception as e_liq:
            logger.debug("liq_check %s: %s", symbol, e_liq)



    _fj_running = False  # reset lock anti-overlap
# ── Daily digest: riepilogo giornaliero alle 08:00 IT ──────────────────────────

async def daily_digest_job(context):
    """Invia digest mattutino alle 08:00 ora italiana."""
    bot: Bot = context.bot
    bot_data = context.bot_data
    now_it = datetime.now(TZ_IT).strftime("%d/%m/%Y %H:%M")

    try:
        positions = await bc.get_positions()
        wallet    = await bc.get_wallet()
    except Exception as e:
        logger.error("daily_digest: errore fetch dati: %s", e)
        return

    n_pos   = len(positions)
    equity  = wallet.get("equity", 0)
    upnl    = wallet.get("upnl", 0)
    rpnl    = wallet.get("realisedPnl", 0)
    margin  = wallet.get("margin", 0)
    alerts  = bot_data.get("alerts_sent", 0)

    lines = [f"☀️ *DIGEST GIORNALIERO — {now_it}*", ""]
    lines.append(f"💼 Equity: `{equity:.2f} USDT`")
    lines.append(f"📈 Unrealised PnL: `{upnl:+.2f} USDT`")
    lines.append(f"💰 Realised PnL: `{rpnl:+.2f} USDT`")
    lines.append(f"🔐 Margine usato: `{margin:.2f} USDT`")
    lines.append(f"📂 Posizioni aperte: `{n_pos}`")
    lines.append(f"🔔 Alert inviati oggi: `{alerts}`")

    if positions:
        lines.append("")
        lines.append("*Posizioni aperte:*")
        for p in positions[:10]:
            sym  = p.get("symbol", "")
            side = "🟢 LONG" if p.get("side") == "Buy" else "🔴 SHORT"
            pnl  = float(p.get("unrealisedPnl", 0))
            sign = "+" if pnl >= 0 else ""
            lines.append(f"  • {sym} {side} `{sign}{pnl:.2f} USDT`")

    await send_alert(bot, "\n".join(lines))
    # Reset contatore alert giornaliero
    bot_data["alerts_sent"] = 0


# ── WebSocket liquidazioni: callback ─────────────────────────────────────────
_bot_ref: Bot | None = None


async def liquidation_callback(msg: str):
    if _bot_ref:
        await send_alert(_bot_ref, msg)


# ── post_init: caricamento dati al boot ──────────────────────────────────────
async def post_init(app):
    global _bot_ref
    _bot_ref = app.bot

    app.bot_data["uptime_start"]  = datetime.now(TZ_IT)
    app.bot_data["alerts_sent"]   = 0
    app.bot_data["monitoring"]    = False
    app.bot_data["symbols_count"] = 0

    use_dynamic = os.getenv("USE_DYNAMIC_THRESHOLDS", "false").lower() == "true"
    window_h    = int(os.getenv("DYNAMIC_WINDOW_HOURS", 24))
    logger.info(
        "Soglie: %s | Finestra rolling: %dH",
        "IBRIDE (fisse + dinamiche)" if use_dynamic else "FISSE",
        window_h,
    )

    # 1. Carica watchlist persistente
    logger.info("Caricamento watchlist persistente...")
    wm.load()

    # Migrazione one-shot: importa credenziali .env in user_store multi-user
    if _user_store.migrate_from_env():
        logger.info("post_init: credenziali .env migrate nel multi-user store")

    # 1b. Carica storico guadagni funding
    ft.load()

    # 2. Carica cap funding (one-time al boot)
    logger.info("Caricamento instruments info...")
    caps = await bc.get_instruments_info()
    al.set_symbol_caps(caps)

    # 2. Recupera simboli per WebSocket liquidazioni
    logger.info("Recupero simboli attivi...")
    tickers = await bc.get_funding_tickers()
    symbols = [t["symbol"] for t in tickers]

    if symbols:
        logger.info("Avvio WebSocket liquidazioni su %d simboli...", len(symbols))
        asyncio.create_task(
            wsl.run_liquidation_ws(liquidation_callback, symbols=symbols)
        )

    # ── Registra comandi e Menu Button Telegram ───────────────────────────
    await _setup_bot_menu(app.bot)


# ── Setup Menu Button + comandi Telegram ─────────────────────────────────────
async def _setup_bot_menu(bot):
    """Registra i comandi e attiva il Menu Button (☰) accanto alla barra di testo."""
    from telegram import BotCommand, MenuButtonCommands

    bot_commands = [
        BotCommand("start",          "🚀 Avvia il bot e configura le API"),
        BotCommand("help",           "📋 Lista completa dei comandi"),
        BotCommand("status",         "📡 Stato bot e monitoraggio attivo"),
        BotCommand("top10",          "🔥 Top 10 SHORT + LONG in tempo reale"),
        BotCommand("funding_top",    "📈 Top 10 funding positivi (SHORT)"),
        BotCommand("funding_bottom", "📉 Top 10 funding negativi (LONG)"),
        BotCommand("storico",        "🕐 Ultimi 8 cicli di un simbolo"),
        BotCommand("storico7g",      "📊 Storico 7 giorni con grafici"),
        BotCommand("backtest",       "🧪 Simula P&L 30gg (SYMBOL|top10|watchlist)"),
        BotCommand("watchlist",      "👁 Stato watchlist e simboli monitorati"),
        BotCommand("watch",          "➕ Aggiungi simboli alla watchlist"),
        BotCommand("unwatch",        "➖ Rimuovi simboli dalla watchlist"),
        BotCommand("mute",           "🔇 Silenzia alert per un simbolo"),
        BotCommand("unmute",         "🔔 Riattiva alert per un simbolo"),
        BotCommand("alerts",         "⚙️ Soglie custom per simbolo"),
        BotCommand("saldo",          "💼 Saldo wallet Bybit"),
        BotCommand("posizioni",      "📂 Posizioni aperte con PnL"),
        BotCommand("test",           "🔧 Test connessione Bybit + Telegram"),
        BotCommand("rischio",        "⚠️ Analisi rischio posizioni aperte"),
        BotCommand("summary",        "📊 Riepilogo rapido portafoglio"),
        BotCommand("newlistings",    "🆕 Nuovi listing con funding elevato"),
        BotCommand("analytics",      "📈 Analytics avanzati e statistiche"),
        BotCommand("alert_config",   "⚙️ Configura soglie alert"),
        BotCommand("profitto_funding","💹 Guadagni da funding per posizioni aperte"),
    ]

    try:
        await bot.set_my_commands(bot_commands)
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logger.info("Menu Button e %d comandi registrati su Telegram.", len(bot_commands))
    except Exception as exc:
        logger.warning("_setup_bot_menu: %s", exc)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        raise ValueError(
            "TELEGRAM_TOKEN non impostato.\n"
            "Aggiungi il token nel file .env e riavvia il bot."
        )

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )


    # Scheduled: daily digest alle 08:00 IT
    from datetime import time as dt_time
    app.job_queue.run_daily(
        daily_digest_job,
        time=dt_time(hour=8, minute=0, second=0, tzinfo=TZ_IT),
        name="daily_digest",
    )
    logger.info("Daily digest schedulato alle 08:00 IT")

    commands.register(app)

    app.job_queue.run_repeating(
        funding_job,
        interval=JOB_INTERVAL,
        first=10,
        name="funding_monitor",
    )

    logger.info(
        "🚀 Funding King Bot avviato — interval=%ds | soglie=%s",
        JOB_INTERVAL,
        "ibride" if os.getenv("USE_DYNAMIC_THRESHOLDS","false").lower()=="true" else "fisse",
    )
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
