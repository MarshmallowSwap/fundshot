"""
bot.py — Funding King Bot
Entry point principale: avvio bot Telegram, job di monitoraggio funding,
WebSocket liquidazioni, auto-trading (FundingTrader).
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
from chart_gen import generate_chart
try:
    import oi_monitor
except Exception as _e:
    oi_monitor = None

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

# ── Auto-trading ──────────────────────────────────────────────────────────────
from trader import CONFIG as TRADER_CONFIG, load_config, BybitTrader, FundingTrader

# Istanze globali del trader (inizializzate in post_init)
_bybit_trader:   BybitTrader   | None = None
_funding_trader: FundingTrader | None = None

# ── Configurazione ────────────────────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID      = os.getenv("CHAT_ID", "")
JOB_INTERVAL = int(os.getenv("JOB_INTERVAL", 60))

# Abilita/disabilita auto-trading via env (default OFF per sicurezza)
TRADING_ENABLED = os.getenv("AUTO_TRADING", "false").lower() == "true"
TRADING_TESTNET = os.getenv("TRADING_TESTNET", "false").lower() == "true"
TRADING_DEMO = os.getenv("TRADING_DEMO", "false").lower() == "true"


# ── Helper: invia messaggio Telegram ─────────────────────────────────────────
async def send_alert(bot: Bot, text: str, target_chat_id=None, symbol: str = None, rate: float = None):
    """Invia alert a un utente specifico o a tutti gli utenti con credenziali.
    Se symbol e rate sono forniti, invia il grafico candlestick insieme all'alert.
    """
    if target_chat_id:
        recipients = [str(target_chat_id)]
    else:
        recipients = _user_store.users_with_credentials()
        if not recipients:
            fallback = os.getenv("CHAT_ID", CHAT_ID)
            if fallback:
                recipients = [fallback]

    # Genera grafico se disponibile
    chart_buf = None
    if symbol and rate is not None:
        try:
            chart_buf = generate_chart(symbol, rate)
            if chart_buf:
                logger.info("Grafico generato per %s (%d bytes)", symbol, len(chart_buf.getvalue()))
            else:
                logger.warning("Grafico None per %s — invio solo testo", symbol)
        except Exception as e:
            logger.warning("Grafico non generato per %s: %s", symbol, e)

    for cid in recipients:
        try:
            if chart_buf:
                chart_buf.seek(0)
                await bot.send_photo(
                    chat_id=cid,
                    photo=chart_buf,
                    caption=text,
                    parse_mode="Markdown",
                )
            else:
                await bot.send_message(chat_id=cid, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error("Errore invio alert a %s: %s", cid, e)


# ── Helper: invia messaggio all'owner (chat_id principale) ───────────────────
async def send_to_owner(bot: Bot, text: str):
    """Invia al CHAT_ID principale (owner del bot)."""
    chat_id = os.getenv("CHAT_ID", CHAT_ID)
    if chat_id:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error("Errore invio owner: %s", e)


# ── Alert liquidazione imminente ──────────────────────────────────────────────
_LIQ_ALERT_SENT: dict[str, float] = {}   # symbol → last liq% when alert sent
LIQ_WARN_PCT = 15.0                       # alert se margine residuo < 15%

async def _check_liq_and_level(bot: Bot, symbol: str, mark_price: float,
                                liq_price: float, side: str, rate_pct: float,
                                bot_data: dict):
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
        del _LIQ_ALERT_SENT[symbol]


# ── Funding rate getter per FundingTrader ─────────────────────────────────────
_funding_cache: dict[str, float] = {}   # aggiornato ogni ciclo del funding_job

async def _get_funding_rate(symbol: str) -> float | None:
    """Ritorna il funding rate corrente dalla cache (aggiornata ogni 60s)."""
    return _funding_cache.get(symbol)

# ── Monitoring pre-trade: simboli candidati all'apertura ──────────────────────
import time as _time
_monitoring: dict = {}   # {symbol: {rate, level, since, direction}}
_MON_FILE = '/tmp/fk_monitoring.json'

def _mon_save():
    try:
        import json
        with open(_MON_FILE, 'w') as f:
            json.dump(_monitoring, f)
    except Exception:
        pass

def _mon_add(symbol: str, rate: float, level: str):
    if symbol not in _monitoring:
        direction = 'SHORT' if rate > 0 else 'LONG'
        _monitoring[symbol] = {
            'rate': round(rate * 100, 4),
            'level': level,
            'direction': direction,
            'since': int(_time.time()),
        }
        _mon_save()
        logger.info("🔍 Monitoring aggiunto: %s %s %+.4f%%", symbol, direction, rate * 100)

def _mon_remove(symbol: str, reason: str = ''):
    if symbol in _monitoring:
        del _monitoring[symbol]
        _mon_save()
        logger.info("🔍 Monitoring rimosso: %s (%s)", symbol, reason)

# ── Job principale: monitoraggio funding ──────────────────────────────────────
_prev_rates: dict[str, float] = {}
_fj_running = False   # lock anti-sovrapposizione

async def funding_job(context):
    global _fj_running
    if _fj_running:
        logger.warning("⚠️ funding_job: job precedente ancora in esecuzione, skip")
        return
    _fj_running = True
    bot: Bot = context.bot
    bot_data = context.bot_data

    try:
        tickers = await bc.get_funding_tickers()
    except Exception as e:
        logger.error("funding_job: errore fetch tickers: %s", e)
        _fj_running = False
        return

    bot_data["symbols_count"] = len(tickers)
    bot_data["monitoring"]    = True
    bot_data["last_cycle"]    = datetime.now(TZ_IT).strftime("%d/%m/%Y %H:%M:%S %Z")

    if not tickers:
        logger.warning("Nessun ticker ricevuto.")
        _fj_running = False
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
        last_price      = float(ticker.get("lastPrice", 0))
        pct_24h         = float(ticker.get("price24hPcnt", 0)) * 100

        # ── Aggiorna cache funding (usata da FundingTrader) ──────────────────
        _funding_cache[symbol] = rate_raw

        # ── Aggiorna storico rolling (per soglie dinamiche) ──────────────────
        al.update_rate_history(symbol, rate_pct)

        # 1. Alert funding rate
        alert_text = al.process_funding(symbol, rate_pct, interval_h, last_price=last_price, pct_24h=pct_24h)
        if alert_text:
            await send_alert(bot, alert_text, symbol=symbol, rate=rate_pct)
            bot_data["alerts_sent"] = bot_data.get("alerts_sent", 0) + 1

        # 1b. Alert cambio livello funding
        if _bot_alert_enabled("level_change"):
            level_alert = al.check_level_change(symbol, al.classify(symbol, rate_pct), rate_pct=rate_pct, last_price=last_price, pct_24h=pct_24h)
            if level_alert:
                await send_alert(bot, level_alert)
                bot_data["alerts_sent"] = bot_data.get("alerts_sent", 0) + 1

        # 2. Alert prossimo funding (entro X minuti)
        if next_funding_ts:
            next_text = al.process_next_funding(symbol, rate_pct, interval_h, next_funding_ts, last_price=last_price, pct_24h=pct_24h)
            if next_text:
                await send_alert(bot, next_text)
                bot_data["alerts_sent"] = bot_data.get("alerts_sent", 0) + 1

        # 3. Tracking guadagno funding: rileva reset ciclo (rate quasi zero)
        RESET_THR = al.RESET_THRESHOLD
        HIGH_THR  = 0.50
        prev_rate = _prev_rates.get(symbol, 0.0)
        _prev_rates[symbol] = rate_pct

        if abs(rate_pct) <= RESET_THR and abs(prev_rate) >= HIGH_THR and al.is_funded(symbol):
            try:
                positions = await bc.get_positions()
                pos = next((p for p in positions if p["symbol"] == symbol), None)
                if pos:
                    size       = float(pos.get("size", 0))
                    mark_price = float(pos.get("markPrice", 0))
                    side       = pos.get("side", "Buy")
                    level      = al.classify(symbol, prev_rate) if abs(prev_rate) > 0 else "high"
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
                        sign     = "+" if gain >= 0 else ""
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

        # 4. Alert liquidazione imminente
        try:
            pos_liq = next((p for p in positions_all if p.get("symbol") == symbol), None)
            if pos_liq and float(pos_liq.get("size", 0)) > 0:
                if _bot_alert_enabled("liquidation"):
                    await _check_liq_and_level(
                        bot, symbol,
                        float(pos_liq.get("markPrice", 0)),
                        float(pos_liq.get("liqPrice", 0) or 0),
                        pos_liq.get("side", "Buy"),
                        rate_pct, bot_data,
                    )
        except Exception as e_liq:
            logger.debug("liq_check %s: %s", symbol, e_liq)

    # ── Aggiorna _monitoring per TUTTI i simboli (non solo watchlist) ─────────
    open_symbols = set(_funding_trader.positions.keys()) if _funding_trader else set()
    for ticker in tickers:
        sym      = ticker.get("symbol", "")
        r_raw    = float(ticker.get("fundingRate", 0))
        _funding_cache[sym] = r_raw  # cache completa per tutti
        abs_r    = abs(r_raw * 100)
        lvl      = None
        if abs_r >= 2.00:   lvl = "hard"
        elif abs_r >= 1.50: lvl = "extreme"
        elif abs_r >= 1.00: lvl = "high"
        elif abs_r >= 0.50: lvl = "base"
        if lvl and sym not in open_symbols:
            _mon_add(sym, r_raw, lvl)
        elif sym in _monitoring and not lvl:
            _mon_remove(sym, "funding rientrato")

    _fj_running = False


# ── Job OI spike (ogni 5 min) ────────────────────────────────────────────────
_oi_running = False

async def oi_spike_job(context):
    """
    Job OI spike: controlla spike OI ogni 5 minuti.
    Controlla solo i simboli con funding significativo (>0.3%) per efficienza.
    Usa thread pool per parallelizzare le chiamate API.
    """
    global _oi_running
    if not oi_monitor:
        return
    if not _bot_alert_enabled("oi_spike"):
        return
    if _oi_running:
        return
    _oi_running = True

    bot: Bot = context.bot
    try:
        # Simboli prioritari — sempre monitorati indipendentemente dal funding
        PRIORITY_SYMBOLS = {
            "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT",
            "XRPUSDT", "DOGEUSDT", "ADAUSDT", "TRXUSDT", "AVAXUSDT",
        }

        # Filtra simboli con funding significativo dalla cache
        MIN_FUNDING_FOR_OI = 0.003  # 0.3% minimo
        funding_candidates = {
            sym for sym, rate in _funding_cache.items()
            if abs(rate) >= MIN_FUNDING_FOR_OI
        }

        # Unione: prioritari + funding significativo
        all_symbols = PRIORITY_SYMBOLS | funding_candidates
        candidates = [{"symbol": sym} for sym in all_symbols]
        logger.info(
            "oi_spike_job: %d simboli (%d prioritari + %d funding >= 0.3%%)",
            len(candidates), len(PRIORITY_SYMBOLS & all_symbols),
            len(funding_candidates - PRIORITY_SYMBOLS)
        )

        if not candidates:
            logger.debug("oi_spike_job: nessun candidato con funding >= 0.3%%")
            return

        logger.info("oi_spike_job: controllo OI su %d simboli (funding >= 0.3%%)", len(candidates))

        # Esegui chiamate OI in parallelo con thread pool
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        def fetch_one(sym_dict):
            return oi_monitor._fetch_oi(sym_dict["symbol"])

        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = await asyncio.gather(*[
                loop.run_in_executor(pool, oi_monitor._fetch_oi, c["symbol"])
                for c in candidates
            ])

        # Processa risultati e aggiorna file OI per dashboard
        import time, json as _json
        now = time.monotonic()
        oi_snapshot = {}

        for sym_dict, oi_data in zip(candidates, results):
            if not oi_data:
                continue
            chg = oi_data["change_5m"]
            sym = sym_dict["symbol"]
            funding = _funding_cache.get(sym, 0) * 100
            is_spike = chg >= oi_monitor.OI_SPIKE_THRESHOLD or chg <= oi_monitor.OI_DROP_THRESHOLD

            # Salva snapshot per la dashboard
            oi_snapshot[sym] = {
                "change_5m": round(chg, 3),
                "oi":        round(oi_data["oi"], 0),
                "funding":   round(funding, 4),
                "spike":     is_spike,
                "ts":        int(time.time()),
            }

            if is_spike:
                if now - oi_monitor._last_oi_alert.get(sym, 0) < oi_monitor.OI_COOLDOWN_SEC:
                    continue
                msg = oi_monitor.format_oi_spike_alert(sym, chg, funding)
                oi_monitor._last_oi_alert[sym] = now
                logger.info("OI spike %s: %+.2f%%", sym, chg)
                await send_alert(bot, msg, symbol=sym, rate=funding)

        # Scrivi snapshot su file per il proxy
        try:
            with open('/tmp/fk_oi.json', 'w') as _f:
                _json.dump(oi_snapshot, _f)
        except Exception as _fe:
            logger.debug("fk_oi.json write error: %s", _fe)

    except Exception as e:
        logger.warning("oi_spike_job error: %s", e)
    finally:
        _oi_running = False


# ── Job auto-trading ──────────────────────────────────────────────────────────
_tj_running = False   # lock anti-sovrapposizione

async def trading_job(context):
    """
    Job auto-trading: valuta segnali di funding e gestisce posizioni aperte.
    Viene eseguito ogni 60s, subito dopo funding_job (offset +5s).
    Attivo solo se AUTO_TRADING=true nel .env e TRADER_CONFIG abilitato.
    """
    global _tj_running
    if not TRADING_ENABLED:
        return
    if _funding_trader is None:
        return
    if _tj_running:
        logger.warning("⚠️ trading_job: job precedente ancora in esecuzione, skip")
        return
    _tj_running = True

    bot: Bot = context.bot
    owner_chat_id = os.getenv("CHAT_ID", CHAT_ID)

    try:
        # 1. Monitora posizioni aperte (trailing, TP, SL)
        if _funding_trader.positions:
            await _funding_trader.monitor_positions(owner_chat_id)

        # 2. Valuta nuovi segnali sui simboli in monitoraggio attivo
        symbols_to_check = list(_monitoring.keys())

        for symbol in symbols_to_check:
            try:
                funding_rate = _funding_cache.get(symbol)
                if funding_rate is None:
                    continue

                # Aggiorna persistenza
                _funding_trader.update_persistence(symbol, funding_rate)

                # Controlla funding exit su posizioni aperte
                await _funding_trader.check_funding_exit(symbol, funding_rate, owner_chat_id)

                # Cerca nuove aperture
                ok, reason = await _funding_trader.should_open(symbol, funding_rate)
                if ok:
                    await _funding_trader.open_trade(symbol, funding_rate, owner_chat_id)
                    context.bot_data["trades_opened"] = context.bot_data.get("trades_opened", 0) + 1
                    _mon_remove(symbol, "trade aperto")
                else:
                    logger.info("trading_job %s: skip — %s", symbol, reason)

            except Exception as e:
                logger.error("trading_job symbol %s: %s", symbol, e)

    except Exception as e:
        logger.error("trading_job outer: %s", e)
    finally:
        _tj_running = False


# ── Comando /stats trading ────────────────────────────────────────────────────
async def cmd_stats(update, context):
    """Mostra statistiche delle operazioni di auto-trading."""
    if _funding_trader is None:
        await update.message.reply_text(
            "⚠️ Auto-trading non attivo.\n"
            "Imposta `AUTO_TRADING=true` nel `.env` e riavvia il bot.",
            parse_mode="Markdown"
        )
        return

    stats = _funding_trader.get_stats()
    if stats.get("trades", 0) == 0:
        await update.message.reply_text(
            "📊 *Statistiche Trading*\n\nNessun trade registrato in questa sessione.",
            parse_mode="Markdown"
        )
        return

    wins   = stats["wins"]
    losses = stats["losses"]
    total  = stats["trades"]
    bar_w  = int(wins / total * 10) if total else 0
    bar_l  = 10 - bar_w
    bar    = "🟢" * bar_w + "🔴" * bar_l

    open_pos = _funding_trader.positions
    open_lines = []
    for sym, pos in open_pos.items():
        side_e = "🔴 SHORT" if pos.direction == "SHORT" else "🟢 LONG"
        open_lines.append(
            f"  • `{sym}` {side_e} | entry `{pos.entry_price:.4f}` | "
            f"lvl `{pos.level.upper()}`"
        )

    msg = (
        f"📊 *Statistiche Auto-Trading*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Trade totali:    `{total}`\n"
        f"✅ Vincenti:        `{wins}`\n"
        f"❌ Perdenti:        `{losses}`\n"
        f"📈 Win rate:        `{stats['win_rate']}%`\n"
        f"{bar}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 PnL totale:      `{stats['total_pnl']:+.2f} USDT`\n"
        f"📈 Media vincita:   `+{stats['avg_win']:.2f} USDT`\n"
        f"📉 Media perdita:   `{stats['avg_loss']:.2f} USDT`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📂 Posizioni aperte: `{stats['open']}`"
    )

    if open_lines:
        msg += "\n" + "\n".join(open_lines)

    env_label = "🎮 DEMO" if TRADING_DEMO else ("🧪 TESTNET" if TRADING_TESTNET else "🔴 MAINNET")
    msg += f"\n━━━━━━━━━━━━━━━━━━\n⚡ Ambiente: `{env_label}`"

    await update.message.reply_text(msg, parse_mode="Markdown")


# ── Comando /posizioni_trader ─────────────────────────────────────────────────
async def cmd_posizioni_trader(update, context):
    """Mostra le posizioni aperte dall'auto-trader con dettagli completi."""
    if _funding_trader is None or not _funding_trader.positions:
        await update.message.reply_text(
            "📂 Nessuna posizione aperta dall'auto-trader.",
            parse_mode="Markdown"
        )
        return

    lines = ["📂 *Posizioni Auto-Trader*\n━━━━━━━━━━━━━━━━━━"]
    for sym, pos in _funding_trader.positions.items():
        duration = (datetime.now(timezone.utc) - pos.opened_at).seconds // 60
        mark = _bybit_trader.get_mark_price(sym) if _bybit_trader else None
        is_short = pos.direction == "SHORT"

        pnl_pct = 0.0
        if mark:
            pnl_pct = ((pos.entry_price - mark) / pos.entry_price * 100) if is_short \
                      else ((mark - pos.entry_price) / pos.entry_price * 100)
        pnl_usdt = pos.notional * (pnl_pct / 100)

        side_e = "🔴 SHORT" if is_short else "🟢 LONG"
        tp1_done = "✅" if pos.tp1_hit else "⏳"

        lines.append(
            f"\n*{sym}* {side_e}\n"
            f"  Entry:     `{pos.entry_price:.4f}`\n"
            f"  Mark:      `{mark:.4f}`\n" if mark else
            f"  Entry:     `{pos.entry_price:.4f}`\n"
            f"  PnL:       `{pnl_usdt:+.4f} USDT` ({pnl_pct:+.2f}%)\n"
            f"  SL:        `{pos.sl_price:.4f}` (-{pos.sl_pct:.1f}%)\n"
            f"  TP1 {tp1_done}:   `{pos.tp1_price:.4f}`\n"
            f"  Trailing:  `{pos.trailing_stop:.4f}` (buf {pos.trailing_buffer:.1f}%)\n"
            f"  Lvl:       `{pos.level.upper()}` | Durata: `{duration} min`"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Daily digest: riepilogo giornaliero alle 08:00 IT ────────────────────────
async def daily_digest_job(context):
    """Invia digest mattutino alle 08:00 ora italiana."""
    bot: Bot = context.bot
    bot_data = context.bot_data
    now_it   = datetime.now(TZ_IT).strftime("%d/%m/%Y %H:%M")

    try:
        positions = await bc.get_positions()
        wallet    = await bc.get_wallet()
    except Exception as e:
        logger.error("daily_digest: errore fetch dati: %s", e)
        return

    n_pos  = len(positions)
    equity = wallet.get("equity", 0)
    upnl   = wallet.get("upnl", 0)
    rpnl   = wallet.get("realisedPnl", 0)
    margin = wallet.get("margin", 0)
    alerts = bot_data.get("alerts_sent", 0)

    lines = [f"☀️ *DIGEST GIORNALIERO — {now_it}*", ""]
    lines.append(f"💼 Equity: `{equity:.2f} USDT`")
    lines.append(f"📈 Unrealised PnL: `{upnl:+.2f} USDT`")
    lines.append(f"💰 Realised PnL: `{rpnl:+.2f} USDT`")
    lines.append(f"🔐 Margine usato: `{margin:.2f} USDT`")
    lines.append(f"📂 Posizioni aperte: `{n_pos}`")
    lines.append(f"🔔 Alert inviati oggi: `{alerts}`")

    # Stats auto-trader (se attivo)
    if _funding_trader and TRADING_ENABLED:
        stats = _funding_trader.get_stats()
        lines.append(f"\n*Auto-Trader oggi:*")
        lines.append(f"  Trade: `{stats.get('trades', 0)}` | "
                     f"PnL: `{stats.get('total_pnl', 0):+.2f} USDT` | "
                     f"Win rate: `{stats.get('win_rate', 0)}%`")

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
    bot_data["alerts_sent"] = 0


# ── WebSocket liquidazioni: callback ──────────────────────────────────────────
_bot_ref: Bot | None = None

async def liquidation_callback(msg: str):
    if _bot_ref:
        await send_alert(_bot_ref, msg)


# ── post_init: caricamento dati al boot ──────────────────────────────────────
async def post_init(app):
    global _bot_ref, _bybit_trader, _funding_trader
    _bot_ref = app.bot

    app.bot_data["uptime_start"]  = datetime.now(TZ_IT)
    app.bot_data["alerts_sent"]   = 0
    app.bot_data["monitoring"]    = False
    app.bot_data["symbols_count"] = 0
    app.bot_data["trades_opened"] = 0

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

    # 3. Recupera simboli per WebSocket liquidazioni
    logger.info("Recupero simboli attivi...")
    tickers = await bc.get_funding_tickers()
    symbols = [t["symbol"] for t in tickers]

    if symbols:
        logger.info("Avvio WebSocket liquidazioni su %d simboli...", len(symbols))
        asyncio.create_task(
            wsl.run_liquidation_ws(liquidation_callback, symbols=symbols)
        )

    # 4. Inizializza auto-trader ───────────────────────────────────────────────
    if TRADING_ENABLED:
        # Carica configurazione dalla dashboard (se esportata)
        load_config("trader_config.json")

        # Recupera API keys: prima dal multi-user store (owner), poi da .env
        api_key    = os.getenv("BYBIT_API_KEY", "")
        api_secret = os.getenv("BYBIT_API_SECRET", "")

        # Prova a leggere dall'owner nel user_store (stesse key usate dai comandi)
        owner_id = os.getenv("CHAT_ID", CHAT_ID)
        if owner_id:
            try:
                user_creds = _user_store.get_credentials(owner_id)
                if user_creds:
                    api_key    = user_creds.get("api_key", api_key)
                    api_secret = user_creds.get("api_secret", api_secret)
            except Exception:
                pass

        if api_key and api_secret:
            _bybit_trader = BybitTrader(
                api_key    = api_key,
                api_secret = api_secret,
                testnet    = TRADING_TESTNET,
                demo       = TRADING_DEMO,
            )

            async def _tg_send(chat_id, msg, symbol=None, rate=None):
                """Wrapper per inviare messaggi Telegram dal trader, con grafico opzionale."""
                try:
                    chart_buf = None
                    if symbol and rate is not None:
                        try:
                            chart_buf = generate_chart(symbol, rate)
                        except Exception as _ce:
                            logger.warning("Grafico trader non generato: %s", _ce)
                    if chart_buf:
                        chart_buf.seek(0)
                        await app.bot.send_photo(
                            chat_id    = chat_id,
                            photo      = chart_buf,
                            caption    = msg,
                            parse_mode = "Markdown",
                        )
                    else:
                        await app.bot.send_message(
                            chat_id    = chat_id,
                            text       = msg,
                            parse_mode = "Markdown",
                        )
                except Exception as e:
                    logger.error("_tg_send: %s", e)

            _funding_trader = FundingTrader(_bybit_trader, _tg_send)

            env_label = "🎮 DEMO" if TRADING_DEMO else ("🧪 TESTNET" if TRADING_TESTNET else "🔴 MAINNET")
            logger.info(
                "🤖 Auto-trader attivo — %s | size=%.0f USDT | leva=%dx | maxpos=%d",
                env_label,
                TRADER_CONFIG["size_usdt"],
                TRADER_CONFIG["leverage"],
                TRADER_CONFIG["max_positions"],
            )

            # Notifica owner al boot
            await send_to_owner(
                app.bot,
                f"🤖 *Auto-Trader attivato*\n"
                f"Ambiente: `{env_label}`\n"
                f"Size: `{TRADER_CONFIG['size_usdt']} USDT` | "
                f"Leva: `{TRADER_CONFIG['leverage']}x` | "
                f"Max pos: `{TRADER_CONFIG['max_positions']}`\n"
                f"Config: `trader_config.json` {'✅' if os.path.exists('trader_config.json') else '⚠️ non trovato (uso defaults)'}"
            )
        else:
            logger.warning(
                "AUTO_TRADING=true ma BYBIT_API_KEY/BYBIT_API_SECRET non configurate. "
                "Trader non avviato."
            )
    else:
        logger.info("Auto-trading DISABILITATO (AUTO_TRADING=false)")

    # 5. Registra comandi e Menu Button Telegram
    await _setup_bot_menu(app.bot)


# ── Setup Menu Button + comandi Telegram ──────────────────────────────────────
async def _setup_bot_menu(bot):
    """Registra i comandi e attiva il Menu Button (☰) accanto alla barra di testo."""
    from telegram import BotCommand, MenuButtonCommands

    bot_commands = [
        BotCommand("start",           "🚀 Avvia il bot e configura le API"),
        BotCommand("help",            "📋 Lista completa dei comandi"),
        BotCommand("status",          "📡 Stato bot e monitoraggio attivo"),
        BotCommand("top10",           "🔥 Top 10 SHORT + LONG in tempo reale"),
        BotCommand("funding_top",     "📈 Top 10 funding positivi (SHORT)"),
        BotCommand("funding_bottom",  "📉 Top 10 funding negativi (LONG)"),
        BotCommand("storico",         "🕐 Ultimi 8 cicli di un simbolo"),
        BotCommand("storico7g",       "📊 Storico 7 giorni con grafici"),
        BotCommand("backtest",        "🧪 Simula P&L 30gg (SYMBOL|top10|watchlist)"),
        BotCommand("watchlist",       "👁 Stato watchlist e simboli monitorati"),
        BotCommand("watch",           "➕ Aggiungi simboli alla watchlist"),
        BotCommand("unwatch",         "➖ Rimuovi simboli dalla watchlist"),
        BotCommand("mute",            "🔇 Silenzia alert per un simbolo"),
        BotCommand("unmute",          "🔔 Riattiva alert per un simbolo"),
        BotCommand("alerts",          "⚙️ Soglie custom per simbolo"),
        BotCommand("saldo",           "💼 Saldo wallet Bybit"),
        BotCommand("posizioni",       "📂 Posizioni aperte con PnL"),
        BotCommand("test",            "🔧 Test connessione Bybit + Telegram"),
        BotCommand("rischio",         "⚠️ Analisi rischio posizioni aperte"),
        BotCommand("summary",         "📊 Riepilogo rapido portafoglio"),
        BotCommand("newlistings",     "🆕 Nuovi listing con funding elevato"),
        BotCommand("analytics",       "📈 Analytics avanzati e statistiche"),
        BotCommand("alert_config",    "⚙️ Configura soglie alert"),
        BotCommand("profitto_funding","💹 Guadagni da funding per posizioni aperte"),
        # ── Comandi auto-trading ──
        BotCommand("stats",           "🤖 Statistiche auto-trader"),
        BotCommand("posizioni_trader","📂 Posizioni aperte dall'auto-trader"),
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

    # Job funding monitor (ogni 120s — evita sovrapposizioni)
    FUNDING_INTERVAL = max(JOB_INTERVAL, 120)
    app.job_queue.run_repeating(
        funding_job,
        interval=FUNDING_INTERVAL,
        first=10,
        name="funding_monitor",
    )
    logger.info("📡 Job funding monitor schedulato ogni %ds", FUNDING_INTERVAL)

    # Job auto-trading (stesso intervallo, offset +10s rispetto al funding_job)
    if TRADING_ENABLED:
        app.job_queue.run_repeating(
            trading_job,
            interval=FUNDING_INTERVAL,
            first=20,
            name="trading_monitor",
        )
        logger.info("🤖 Job auto-trading schedulato ogni %ds (first=20s)", FUNDING_INTERVAL)

    # Job OI spike (ogni 5 minuti, offset +20s)
    if oi_monitor:
        app.job_queue.run_repeating(
            oi_spike_job,
            interval=300,   # ogni 5 minuti
            first=20,       # parte 20s dopo l'avvio
            name="oi_spike_monitor",
        )
        logger.info("📊 Job OI spike schedulato ogni 5 min")

    # Registra handler comandi (da commands.py)
    commands.register(app)

    # Registra handler comandi trading inline
    from telegram.ext import CommandHandler
    app.add_handler(CommandHandler("stats",            cmd_stats))
    app.add_handler(CommandHandler("test_oi",          cmd_test_oi))
    app.add_handler(CommandHandler("posizioni_trader", cmd_posizioni_trader))

    logger.info(
        "🚀 Funding King Bot avviato — interval=%ds | soglie=%s | trading=%s",
        JOB_INTERVAL,
        "ibride" if os.getenv("USE_DYNAMIC_THRESHOLDS", "false").lower() == "true" else "fisse",
        "ON (%s)" % ("testnet" if TRADING_TESTNET else "mainnet") if TRADING_ENABLED else "OFF",
    )

    app.run_polling(allowed_updates=["message", "callback_query"])


async def cmd_test_oi(update, context):
    """Invia un alert OI spike di test per verificare il formato."""
    import oi_monitor as _oim
    # Prende un simbolo reale con OI alto per il test
    symbol = "BTCUSDT"
    funding = -0.0312  # funding negativo simulato
    oi_chg  = 3.87     # spike simulato

    msg = _oim.format_oi_spike_alert(symbol, oi_chg, funding)
    msg += "\n\n_⚠️ Questo è un alert di TEST_"

    try:
        from chart_gen import generate_chart
        buf = generate_chart(symbol, funding)
        if buf:
            buf.seek(0)
            await update.message.reply_photo(photo=buf, caption=msg, parse_mode="Markdown")
            return
    except Exception:
        pass
    await update.message.reply_text(msg, parse_mode="Markdown")


if __name__ == "__main__":
    main()
