import os
import logging
import asyncio
import time
from dotenv import load_dotenv
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from commands import (
    # Setup wizard
    start, setup_menu, ask_api_key, save_api_key,
    ask_api_secret, save_api_secret, verify_config,
    inline_command, cancel,
    MENU, WAIT_API_KEY, WAIT_API_SECRET,
    # Comandi
    help_command,
    status_command,
    test_command,
    funding_top,
    funding_bottom,
    saldo_command,
    posizioni_command,
)
from alert_logic import process_funding
from bybit_client import BybitClient

load_dotenv()

BOT_TOKEN      = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = os.getenv("CHAT_ID")
BYBIT_API_KEY  = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

if not BOT_TOKEN:
    raise ValueError(
        "TELEGRAM_TOKEN mancante nel file .env\n"
        "Crea il file .env con: TELEGRAM_TOKEN=il_tuo_token"
    )


# ─── JOB: ciclo funding ──────────────────────────────────────────

async def funding_job(app):
    """Ciclo principale: fetch funding, processa alert, aggiorna bot_data."""
    bybit_client: BybitClient = app.bot_data.get("bybit_client")
    if not bybit_client:
        return

    try:
        tickers = await bybit_client.get_funding_rates()
        if not tickers:
            logging.warning("Nessun dato funding ricevuto.")
            return

        app.bot_data["funding_data"] = tickers
        app.bot_data["last_fetch_time"] = time.time()
        app.bot_data["symbol_count"] = len(tickers)

        # Leggi chat_id aggiornato dal .env (può essere cambiato via wizard)
        chat_id = os.getenv("CHAT_ID") or CHAT_ID
        if not chat_id:
            logging.warning("CHAT_ID non configurato — alert non inviati.")
            return

        for item in tickers:
            symbol   = item["symbol"]
            rate     = item["rate"]
            interval = item["interval"]

            alert = process_funding(symbol, rate, interval)
            if alert:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=alert,
                    parse_mode="Markdown",
                )
                # Conta alert inviati nella sessione
                app.bot_data["alert_count_session"] = (
                    app.bot_data.get("alert_count_session", 0) + 1
                )
                logging.info(f"Alert inviato: {symbol} rate={rate:+.4f}%")

    except Exception as e:
        logging.error(f"Errore nel funding_job: {e}")


# ─── MAIN ────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # ── Bybit client ─────────────────────────────────────────────
    bybit_client = BybitClient(
        api_key=os.getenv("BYBIT_API_KEY"),
        api_secret=os.getenv("BYBIT_API_SECRET"),
    )
    app.bot_data["bybit_client"] = bybit_client
    app.bot_data["start_time"] = time.time()
    app.bot_data["alert_count_session"] = 0
    app.bot_data["funding_data"] = []
    app.bot_data["symbol_count"] = 0

    # ── ConversationHandler: setup wizard ────────────────────────
    setup_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(setup_menu, pattern="^setup$"),
        ],
        states={
            MENU: [
                CallbackQueryHandler(ask_api_key,     pattern="^set_apikey$"),
                CallbackQueryHandler(ask_api_secret,  pattern="^set_apisecret$"),
                CallbackQueryHandler(verify_config,   pattern="^verify$"),
                CallbackQueryHandler(inline_command,  pattern="^cmd_"),
                CallbackQueryHandler(setup_menu,      pattern="^setup$"),
            ],
            WAIT_API_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_api_key),
            ],
            WAIT_API_SECRET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_api_secret),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(setup_conv)

    # ── Comandi standard ─────────────────────────────────────────
    app.add_handler(CommandHandler("help",            help_command))
    app.add_handler(CommandHandler("status",          status_command))
    app.add_handler(CommandHandler("test",            test_command))
    app.add_handler(CommandHandler("funding_top",     funding_top))
    app.add_handler(CommandHandler("funding_bottom",  funding_bottom))
    app.add_handler(CommandHandler("saldo",           saldo_command))
    app.add_handler(CommandHandler("posizioni",       posizioni_command))

    # ── Job periodico: funding ogni 60s ──────────────────────────
    app.job_queue.run_repeating(
        lambda ctx: asyncio.ensure_future(funding_job(app)),
        interval=60,
        first=5,
    )

    logging.info("🚀 Funding King Bot avviato!")
    app.run_polling()


if __name__ == "__main__":
    main()
