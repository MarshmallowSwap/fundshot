"""
bot.py — Funding King Bot
Entry point principale: avvio bot Telegram, job di monitoraggio, WebSocket liquidazioni.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Bot
from telegram.ext import ApplicationBuilder

import bybit_client as bc
import alert_logic as al
import commands
import ws_liquidations as wsl
import watchlist_manager as wm

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
async def send_alert(bot: Bot, text: str):
    chat_id = os.getenv("CHAT_ID", CHAT_ID)
    if not chat_id:
        logger.warning("CHAT_ID non impostato, alert non inviato.")
        return
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error("Errore invio alert: %s", e)


# ── Job principale: monitoraggio funding ──────────────────────────────────────
async def funding_job(context):
    bot: Bot  = context.bot
    bot_data  = context.bot_data

    try:
        tickers = await bc.get_funding_tickers()
    except Exception as e:
        logger.error("funding_job: errore fetch tickers: %s", e)
        return

    bot_data["symbols_count"] = len(tickers)
    bot_data["monitoring"]    = True
    bot_data["last_cycle"]    = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S UTC")

    if not tickers:
        logger.warning("Nessun ticker ricevuto.")
        return

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

        # 2. Alert prossimo funding (entro X minuti)
        if next_funding_ts:
            next_text = al.process_next_funding(symbol, rate_pct, interval_h, next_funding_ts)
            if next_text:
                await send_alert(bot, next_text)
                bot_data["alerts_sent"] = bot_data.get("alerts_sent", 0) + 1

        # 3. Alert PUMP/DUMP prezzo
        try:
            lp         = float(last_price)
            pp1h       = float(prev_price_1h)
            var_1h_raw = str((lp - pp1h) / pp1h) if pp1h > 0 else "0"
        except (ValueError, ZeroDivisionError):
            var_1h_raw = "0"

        pump_text = al.process_pump_dump(symbol, var_1h_raw, str(float(pct_24h)), last_price)
        if pump_text:
            await send_alert(bot, pump_text)
            bot_data["alerts_sent"] = bot_data.get("alerts_sent", 0) + 1


# ── WebSocket liquidazioni: callback ─────────────────────────────────────────
_bot_ref: Bot | None = None


async def liquidation_callback(msg: str):
    if _bot_ref:
        await send_alert(_bot_ref, msg)


# ── post_init: caricamento dati al boot ──────────────────────────────────────
async def post_init(app):
    global _bot_ref
    _bot_ref = app.bot

    app.bot_data["uptime_start"]  = datetime.now(timezone.utc)
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
