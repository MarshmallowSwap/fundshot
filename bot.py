"""
bot.py — FundShot Bot
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

# ── SaaS multi-tenant ─────────────────────────────────────────────────────────
import onboarding
from user_registry import registry as _registry
from trading_manager import trading_manager as _trading_manager

# ── Auto-trading ──────────────────────────────────────────────────────────────
from trader import CONFIG as TRADER_CONFIG, load_config, BybitTrader, BinanceFuturesTrader, FundingTrader

# Istanze globali del trader (inizializzate in post_init)
_bybit_trader:   BybitTrader   | None = None
_funding_trader: FundingTrader | None = None
# Multi-exchange traders: { exchange: FundingTrader }
_exchange_traders: dict[str, FundingTrader] = {}

# ── Configurazione ────────────────────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID      = os.getenv("CHAT_ID", "")
CHANNEL_ID   = os.getenv("CHANNEL_ID", "")   # Telegram channel pubblico (es. @FundShot_channel)
JOB_INTERVAL = int(os.getenv("JOB_INTERVAL", 60))

# Abilita/disabilita auto-trading via env (default OFF per sicurezza)
TRADING_ENABLED = os.getenv("AUTO_TRADING", "false").lower() == "true"
TRADING_TESTNET = os.getenv("TRADING_TESTNET", "false").lower() == "true"
TRADING_DEMO = os.getenv("TRADING_DEMO", "false").lower() == "true"


# ── Helper: invia messaggio Telegram ─────────────────────────────────────────
# ── Alert counter per Free users ──────────────────────────────────────────────
# { chat_id_str → {"count": int, "date": "YYYY-MM-DD", "warned": bool} }
_alert_counters: dict[str, dict] = {}
FREE_ALERT_LIMIT = 10


def _check_alert_limit(chat_id_str: str, plan: str) -> tuple[bool, bool]:
    """
    Controlla il limite alert giornaliero per utenti Free.
    Ritorna (can_send, show_upgrade_msg).
    - can_send=True  → invia l'alert normalmente
    - can_send=False → non inviare l'alert
    - show_upgrade_msg=True → invia il messaggio di upgrade (solo una volta al giorno)
    """
    if plan != "free":
        return True, False

    today = datetime.now(TZ_IT).strftime("%Y-%m-%d")
    entry = _alert_counters.get(chat_id_str)

    # Reset se nuovo giorno
    if not entry or entry["date"] != today:
        _alert_counters[chat_id_str] = {"count": 0, "date": today, "warned": False}
        entry = _alert_counters[chat_id_str]

    entry["count"] += 1

    if entry["count"] <= FREE_ALERT_LIMIT:
        return True, False  # dentro il limite

    # Oltre il limite
    if not entry["warned"]:
        entry["warned"] = True
        return False, True  # mostra upgrade msg una volta

    return False, False  # silenzio dopo il primo warning


async def send_alert(bot: Bot, text: str, target_chat_id=None, symbol: str = None,
                     rate: float = None, exchange: str = None):
    """Invia alert a un utente specifico o a tutti gli utenti registrati su Supabase."""
    # Badge exchange ora è solo nel footer (_exchange_footer) — non aggiungiamo più header
    if target_chat_id:
        recipients = [str(target_chat_id)]
    else:
        # Multi-tenant: tutti i chat_id con almeno un exchange configurato
        supabase_ids = [str(cid) for cid in _registry.chat_ids()]
        if supabase_ids:
            recipients = supabase_ids
        else:
            # Fallback legacy: user_store o CHAT_ID env
            recipients = _user_store.users_with_credentials()
            if not recipients:
                fallback = os.getenv("CHAT_ID", CHAT_ID)
                if fallback:
                    recipients = [fallback]

    # Genera grafico se disponibile
    chart_buf = None
    if symbol and rate is not None:
        try:
            chart_buf = generate_chart(symbol, rate, exchange=exchange or "bybit")
            if chart_buf:
                logger.info("Grafico generato per %s/%s (%d bytes)", exchange or "bybit", symbol, len(chart_buf.getvalue()))
            else:
                logger.warning("Grafico None per %s — invio solo testo", symbol)
        except Exception as e:
            logger.warning("Grafico non generato per %s: %s", symbol, e)

    for cid in recipients:
        try:
            # ── Controlla piano e limite alert ───────────────────────────────
            try:
                from db.supabase_client import get_user
                from datetime import timezone
                user = await get_user(int(cid))
                user_plan = "free"
                if user and user.plan != "free":
                    # Verifica scadenza
                    from db.supabase_client import get_client as _gc
                    _res = _gc().table("users").select("plan_expires_at").eq("id", user.id).single().execute()
                    _exp = (_res.data or {}).get("plan_expires_at")
                    if _exp:
                        from datetime import datetime as _dt
                        _exp_dt = _dt.fromisoformat(_exp.replace("Z", "+00:00"))
                        if _dt.now(timezone.utc) <= _exp_dt:
                            user_plan = user.plan
                        # else: piano scaduto → free
                    else:
                        user_plan = user.plan
            except Exception:
                user_plan = "free"

            can_send, show_upgrade = _check_alert_limit(cid, user_plan)

            if show_upgrade:
                await bot.send_message(
                    chat_id=cid,
                    text=(
                        "⚡ *You've reached your 10 free alerts for today.*\n\n"
                        "Upgrade to *Pro* to get unlimited alerts, auto-trading and more.\n\n"
                        "👉 Use /upgrade to activate Pro from $15/month."
                    ),
                    parse_mode="Markdown",
                )
                continue

            if not can_send:
                continue

            # ── Invia alert ───────────────────────────────────────────────────
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

# Alert history in memoria (ultimi 100 alert)
_alert_history: list[dict] = []
MAX_ALERT_HISTORY = 100

def _save_alert_history(symbol: str, level: str, rate_pct: float, exchange: str, text: str):
    """Salva alert in memoria e su file per la dashboard."""
    import json as _ahj, os as _aho
    entry = {
        "symbol":   symbol,
        "level":    level,
        "rate_pct": round(rate_pct, 4),
        "exchange": exchange,
        "ex_em":    {"bybit":"🟡","binance":"🟠","okx":"🔵","hyperliquid":"🟣"}.get(exchange.lower(),"⚡"),
        "ts":       int(time.time()),
        "preview":  text[:120] if text else "",
    }
    _alert_history.append(entry)
    if len(_alert_history) > MAX_ALERT_HISTORY:
        _alert_history.pop(0)
    # Scrivi su file per proxy
    try:
        f = "/tmp/fs_alert_history.json"
        existing = []
        if _aho.path.exists(f):
            try:
                existing = _ahj.loads(open(f).read())
            except Exception:
                existing = []
        existing.append(entry)
        existing = existing[-MAX_ALERT_HISTORY:]
        open(f, "w").write(_ahj.dumps(existing))
    except Exception:
        pass
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
_funding_cache_tickers: dict[str, object] = {}  # FundingTicker per symbol — per TradingManager

async def _get_funding_rate(symbol: str) -> float | None:
    """Ritorna il funding rate corrente dalla cache (aggiornata ogni 60s)."""
    return _funding_cache.get(symbol)

# ── Monitoring pre-trade: simboli candidati all'apertura ──────────────────────
import time as _time
_monitoring: dict = {}   # {symbol: {rate, level, since, direction}}
_MON_FILE = '/tmp/fs_monitoring.json'

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

async def _process_exchange_tickers(
    bot: Bot,
    bot_data: dict,
    tickers: list,          # list[FundingTicker] dal client exchange
    exchange: str,          # "bybit" | "binance" | "okx"
    positions_all: list,    # posizioni pre-fetchate per questo exchange
    target_chat_ids: list,  # chat_id degli utenti con questo exchange configurato
    env: str = "mainnet",   # "mainnet" | "testnet"
):
    """
    Processa i ticker di un singolo exchange: alert funding, level change,
    pre-settlement, liquidazione. Invia alert solo agli utenti di quell'exchange.
    """
    from exchanges.models import FundingTicker as _FT

    for ticker in tickers:
        # FundingTicker dataclass
        symbol       = ticker.symbol
        rate_raw     = ticker.funding_rate          # già float
        rate_pct     = rate_raw * 100
        interval_h   = ticker.funding_interval_h
        next_ts      = ticker.next_funding_time
        last_price   = ticker.last_price
        pct_24h      = ticker.price_24h_pct * 100 if abs(ticker.price_24h_pct) < 1 else ticker.price_24h_pct

        if not commands.is_watched(symbol):
            continue

        # Cache funding per TradingManager (solo Bybit per ora)
        if exchange == "bybit":
            _funding_cache[symbol] = rate_raw
            _funding_cache_tickers[symbol] = ticker

        al.update_rate_history(symbol, rate_pct)

        # Costruisce kwargs per send_alert con exchange badge
        ex_kwargs = {"exchange": exchange}

        # 1. Alert funding rate
        alert_text = al.process_funding(symbol, rate_pct, interval_h, last_price=last_price, pct_24h=pct_24h, exchange=exchange, env=env)
        if alert_text:
            level = al.classify(symbol, rate_pct)
            for cid in target_chat_ids:
                if al.should_send_to_user(str(cid), level, symbol, exchange):
                    await send_alert(bot, alert_text, target_chat_id=cid, symbol=symbol, rate=rate_pct, **ex_kwargs)
            bot_data["alerts_sent"] = bot_data.get("alerts_sent", 0) + 1
            _save_alert_history(symbol, level, rate_pct, exchange, alert_text)
            # Channel pubblico: solo HARD/JACKPOT (>=2%) con CTA abbonamento
            if level in ("hard", "critico") and os.getenv("CHANNEL_ID", CHANNEL_ID):
                try:
                    _ex_em_ch = {"bybit": "🟡", "binance": "🟠", "hyperliquid": "🟣"}.get(exchange, "⚡")
                    _cta = (
                        alert_text + "\n\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        "🤖 *Auto-trader attivato* per gli iscritti Pro/Elite\n"
                        "👉 [Attiva FundShot](https://t.me/FundShot_bot?start=upgrade_pro) "
                        "per ricevere TUTTI gli alert in tempo reale"
                    )
                    _chart_ch = generate_chart(symbol, rate_pct, exchange=exchange)
                    await send_to_channel(context.bot, _cta, photo_buf=_chart_ch)
                except Exception as _ce:
                    logger.debug("channel send: %s", _ce)

        # ── AUTO-TRADER: apri posizione se configurato per questo exchange ──
        if TRADING_ENABLED and exchange in _exchange_traders:
            ft = _exchange_traders[exchange]
            ft.update_persistence(symbol, rate_pct / 100)
            ok, reason = await ft.should_open(symbol, rate_pct / 100)
            if ok:
                owner_cid = os.getenv("CHAT_ID", CHAT_ID)
                if owner_cid:
                    await ft.open_trade(symbol, rate_pct / 100, owner_cid)
                # Channel: avvisa quando il bot apre un trade HARD+ (anche se alert già inviato)
                _trade_level = ft.get_level(rate_pct / 100)
                if _trade_level in ("hard", "critico") and os.getenv("CHANNEL_ID", CHANNEL_ID):
                    try:
                        _ex_em_t = {"bybit": "🟡", "binance": "🟠", "hyperliquid": "🟣"}.get(exchange, "⚡")
                        _dir_t   = "SHORT 📉" if rate_pct > 0 else "LONG 📈"
                        _cta_t   = (
                            f"🤖 *Trade aperto — {_dir_t}*\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"📌 `{symbol}` · {_ex_em_t}\n"
                            f"📊 Funding: `{rate_pct:+.4f}%` ({_trade_level.upper()})\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"🤖 Il bot ha appena aperto questa posizione in automatico\n"
                            f"👉 [Attiva FundShot](https://t.me/FundShot_bot?start=upgrade_pro) "
                            f"per il tuo auto-trader"
                        )
                        _chart_t = generate_chart(symbol, rate_pct, exchange=exchange)
                        await send_to_channel(context.bot, _cta_t, photo_buf=_chart_t)
                        logger.info("Channel: trade HARD aperto %s %s", exchange, symbol)
                    except Exception as _tce:
                        logger.debug("channel trade: %s", _tce)

        # 1b. Alert cambio livello
        if _bot_alert_enabled("level_change"):
            level_alert = al.check_level_change(
                symbol, al.classify(symbol, rate_pct),
                rate_pct=rate_pct, last_price=last_price, pct_24h=pct_24h,
                exchange=exchange, env=env,
            )
            if level_alert:
                level = al.classify(symbol, rate_pct)
                for cid in target_chat_ids:
                    if al.should_send_to_user(str(cid), level, symbol, exchange):
                        await send_alert(bot, level_alert, target_chat_id=cid, symbol=symbol, rate=rate_pct, **ex_kwargs)
                bot_data["alerts_sent"] = bot_data.get("alerts_sent", 0) + 1

        # 2. Alert pre-settlement (Pro/Elite only)
        if next_ts:
            next_text = al.process_next_funding(symbol, rate_pct, interval_h, next_ts, last_price=last_price, pct_24h=pct_24h, exchange=exchange, env=env)
            if next_text:
                for cid in target_chat_ids:
                    # Controlla piano — solo Pro/Elite ricevono pre-settlement
                    try:
                        from db.supabase_client import get_user, get_client as _gc_ps
                        from datetime import datetime as _dt_ps, timezone as _tz_ps
                        _u_ps = await get_user(int(cid))
                        _plan_ps = "free"
                        if _u_ps and _u_ps.plan != "free":
                            _res_ps = _gc_ps().table("users").select("plan_expires_at").eq("id", _u_ps.id).single().execute()
                            _exp_ps = (_res_ps.data or {}).get("plan_expires_at")
                            if not _exp_ps or _dt_ps.fromisoformat(_exp_ps.replace("Z", "+00:00")) > _dt_ps.now(_tz_ps.utc):
                                _plan_ps = _u_ps.plan
                    except Exception:
                        _plan_ps = "free"

                    if _plan_ps == "free":
                        continue  # Free → skip pre-settlement

                    await send_alert(bot, next_text, target_chat_id=cid, symbol=symbol, rate=rate_pct, **ex_kwargs)
                bot_data["alerts_sent"] = bot_data.get("alerts_sent", 0) + 1

        # 3. Alert liquidazione (solo Bybit per ora — posizioni pre-fetchate)
        if exchange == "bybit":
            try:
                pos_liq = next((p for p in positions_all if p.get("symbol") == symbol), None)
                if pos_liq and float(pos_liq.get("size", 0)) > 0 and _bot_alert_enabled("liquidation"):
                    await _check_liq_and_level(
                        bot, symbol,
                        float(pos_liq.get("markPrice", 0)),
                        float(pos_liq.get("liqPrice", 0) or 0),
                        pos_liq.get("side", "Buy"),
                        rate_pct, bot_data,
                    )
            except Exception as e_liq:
                logger.debug("liq_check %s: %s", symbol, e_liq)

    # Aggiorna _monitoring
    open_symbols = set(_funding_trader.positions.keys()) if _funding_trader else set()
    for ticker in tickers:
        sym   = ticker.symbol
        r_raw = ticker.funding_rate
        abs_r = abs(r_raw * 100)
        lvl   = None
        if abs_r >= 2.00:   lvl = "hard"
        elif abs_r >= 1.50: lvl = "extreme"
        elif abs_r >= 1.00: lvl = "high"
        elif abs_r >= 0.50: lvl = "soft"
        if lvl and sym not in open_symbols:
            _mon_add(sym, r_raw, lvl)
        elif sym in _monitoring and not lvl and exchange == "bybit":
            _mon_remove(sym, "funding rientrato")


async def _init_exchange_traders(bot) -> None:
    """
    Inizializza FundingTrader per ogni exchange con chiavi configurate nel DB.
    Chiamata all'avvio e quando l'autotrader viene attivato dalla dashboard.
    """
    global _exchange_traders

    owner_chat_id = int(os.getenv("CHAT_ID", CHAT_ID or "0"))
    if not owner_chat_id:
        return

    try:
        from db.supabase_client import get_user, get_credentials
        user = await get_user(owner_chat_id)
        if not user:
            return

        async def _make_send_fn(exch_name: str):
            async def _send(chat_id, msg, symbol=None, rate=None):
                try:
                    chart_buf = None
                    if symbol and rate is not None:
                        try:
                            chart_buf = generate_chart(symbol, rate, exchange=exch_name)
                        except Exception:
                            pass
                    if chart_buf:
                        chart_buf.seek(0)
                        await bot.send_photo(chat_id=chat_id, photo=chart_buf,
                                             caption=msg, parse_mode="Markdown")
                    else:
                        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                except Exception as e:
                    logger.error("_send trader %s: %s", exch_name, e)
            return _send

        for exchange in ["bybit", "binance", "hyperliquid"]:
            if exchange in _exchange_traders:
                continue  # già inizializzato
            try:
                cred = await get_credentials(user.id, exchange)
                if not cred or not cred.api_key:
                    continue
                is_demo = cred.environment == "demo"
                if exchange == "bybit":
                    client = BybitTrader(
                        api_key=cred.api_key, api_secret=cred.api_secret,
                        demo=is_demo, testnet=False
                    )
                elif exchange == "binance":
                    client = BinanceFuturesTrader(
                        api_key=cred.api_key, api_secret=cred.api_secret,
                        demo=is_demo
                    )
                elif exchange == "hyperliquid":
                    # Per HL, api_key = wallet address ETH, api_secret = private key
                    # Alert-only per ora — trading non implementato
                    from exchanges.hyperliquid import HyperliquidClient as _HLC
                    client = _HLC(api_key=cred.api_key, api_secret=cred.api_secret)
                else:
                    continue

                send_fn = await _make_send_fn(exchange)
                ft = FundingTrader(client, send_fn, exchange_name=exchange)
                ft.chat_id = str(owner_chat_id)
                _exchange_traders[exchange] = ft
                logger.info("FundingTrader inizializzato: exchange=%s env=%s", exchange, cred.environment)
            except Exception as e:
                logger.warning("_init_exchange_traders %s: %s", exchange, e)

        logger.info("Exchange traders attivi: %s", list(_exchange_traders.keys()))

    except Exception as e:
        logger.error("_init_exchange_traders: %s", e)


_funding_job_cycles = 0

async def funding_job(context):
    global _fj_running, _funding_job_cycles
    if _fj_running:
        logger.warning("⚠️ funding_job: job precedente ancora in esecuzione, skip")
        return
    _fj_running = True
    _funding_job_cycles += 1
    bot: Bot = context.bot
    bot_data = context.bot_data

    bot_data["monitoring"] = True
    bot_data["last_cycle"] = datetime.now(TZ_IT).strftime("%d/%m/%Y %H:%M:%S %Z")

    # Refresh registry ogni 5 cicli (~10 min) per captare nuove API keys
    if _funding_job_cycles % 5 == 1:
        try:
            n = await _registry.refresh()
            logger.info("Registry refreshato: %d client attivi", n)
        except Exception as _re:
            logger.warning("Registry refresh error: %s", _re)

    try:
        # ── Raggruppa utenti per exchange ────────────────────────────────────
        from collections import defaultdict
        exchange_users: dict[str, list] = defaultdict(list)
        for uc in _registry.all_clients():
            exchange_users[uc.exchange].append(str(uc.chat_id))

        # Assicura sempre Bybit come fallback per l'owner
        owner_id = os.getenv("CHAT_ID", CHAT_ID)
        if owner_id and "bybit" not in exchange_users:
            exchange_users["bybit"].append(owner_id)
            logger.warning("Bybit non nel registry — aggiunto fallback owner")

        # Hyperliquid: sempre attivo se non già nel registry (dati pubblici, no API key)
        if owner_id and "hyperliquid" not in exchange_users:
            exchange_users["hyperliquid"].append(owner_id)

        # Fallback: se registry completamente vuoto
        if not exchange_users:
            if owner_id:
                exchange_users["bybit"].append(owner_id)

        total_tickers = 0

        # ── Per ogni exchange attivo → fetch tickers → process alert ─────────
        for exchange, chat_ids in exchange_users.items():
            try:
                # Prendi un client qualsiasi per questo exchange (tutti hanno stessi tickers pubblici)
                uc_list = [uc for uc in _registry.all_clients() if uc.exchange == exchange]
                if not uc_list:
                    # Hyperliquid: usa client pubblico direttamente
                    if exchange == "hyperliquid":
                        from exchanges.hyperliquid import HyperliquidClient as _HLpub
                        _hl_pub = _HLpub()
                        tickers = await _hl_pub.get_funding_tickers()
                        if not tickers:
                            continue
                    # Fallback legacy per Bybit
                    elif exchange == "bybit":
                        raw = await bc.get_funding_tickers()
                        from exchanges.models import FundingTicker as _FT
                        tickers = [
                            _FT(
                                symbol=t.get("symbol",""),
                                funding_rate=float(t.get("fundingRate",0)),
                                next_funding_time=int(t.get("nextFundingTime",0)),
                                funding_interval_h=float(t.get("fundingIntervalHour",8)),
                                last_price=float(t.get("lastPrice",0)),
                                price_24h_pct=float(t.get("price24hPcnt",0)),
                                exchange="bybit",
                            )
                            for t in raw if t.get("symbol","").endswith("USDT")
                        ]
                    else:
                        continue
                else:
                    tickers = await uc_list[0].client.get_funding_tickers()

                if not tickers:
                    logger.warning("funding_job: nessun ticker da %s", exchange)
                    continue

                total_tickers += len(tickers)
                logger.info("funding_job: %d tickers da %s per %d utenti", len(tickers), exchange, len(chat_ids))

                # Pre-fetch posizioni per Bybit (legacy)
                positions_all = []
                if exchange == "bybit":
                    try:
                        positions_all = await bc.get_positions()
                    except Exception as _e_pos:
                        logger.warning("funding_job: errore posizioni %s: %s", exchange, _e_pos)

                await _process_exchange_tickers(
                    bot, bot_data, tickers, exchange, positions_all, chat_ids,
                    env="testnet" if TRADING_TESTNET else "mainnet",
                )

            except Exception as e_ex:
                logger.error("funding_job: errore exchange %s: %s", exchange, e_ex)

        bot_data["symbols_count"] = total_tickers

    except Exception as e:
        logger.error("funding_job outer: %s", e)
    finally:
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
                # Channel pubblico: solo spike OI molto forti (>5% o <-5%)
                _oi_thresh_ch = 5.0
                if abs(chg) >= _oi_thresh_ch and os.getenv("CHANNEL_ID", CHANNEL_ID):
                    _dir_oi = "📈 SPIKE" if chg > 0 else "📉 DROP"
                    _oi_ch_msg = (
                        f"⚡ *OI {_dir_oi} — {sym}*\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"📊 OI Δ5m: `{chg:+.2f}%`\n"
                        f"💰 Funding: `{funding:+.4f}%`\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"🤖 I nostri trader ricevono questo segnale in tempo reale\n"
                        f"👉 [Attiva FundShot](https://t.me/FundShot_bot?start=upgrade_pro)"
                    )
                    try:
                        await send_to_channel(context.bot, _oi_ch_msg)
                    except Exception as _oice:
                        logger.debug("channel OI: %s", _oice)

        # Scrivi snapshot su file per il proxy
        try:
            with open('/tmp/fs_oi.json', 'w') as _f:
                _json.dump(oi_snapshot, _f)
        except Exception as _fe:
            logger.debug("fk_oi.json write error: %s", _fe)

    except Exception as e:
        logger.warning("oi_spike_job error: %s", e)
    finally:
        _oi_running = False


# ── Job auto-trading ──────────────────────────────────────────────────────────
_tj_running = False   # lock anti-sovrapposizione

_last_config_ts: float = 0.0

# { exchange: last_mtime }
_last_config_ts: dict = {}  # resettato ad ogni restart del processo

def _check_config_flag(exchange: str = "bybit") -> dict | None:
    """
    Legge /tmp/fs_config_{exchange}.json scritto dalla dashboard.
    Aggiorna TRADER_CONFIG se il file e piu recente dell ultima lettura.
    Ritorna i dati config se applicati, None altrimenti.
    """
    import json as _j
    cfg_file = f"/tmp/fs_config_{exchange}.json"
    if not os.path.exists(cfg_file):
        cfg_file = "/tmp/fs_config.json"
    try:
        if not os.path.exists(cfg_file):
            return None
        mtime = os.path.getmtime(cfg_file)
        last  = _last_config_ts.get(exchange, 0.0)
        if mtime <= last:
            return None
        _last_config_ts[exchange] = mtime
        data = _j.loads(open(cfg_file).read())
        data_exchange = data.get("exchange", "bybit")
        if data_exchange != exchange and data_exchange != "":
            return None
        tmp = f"/tmp/fs_config_applied_{exchange}.json"
        with open(tmp, "w") as _f:
            _j.dump(data, _f)
        from trader import load_config as _lc
        _lc(tmp)
        logger.info(
            "Config aggiornata exchange=%s source=%s size=%.0f leva=%d maxpos=%d sl=%.1f",
            exchange, data.get("_source","?"),
            data.get("mm",{}).get("size", data.get("size", 0)),
            data.get("mm",{}).get("leva", data.get("leva", 0)),
            data.get("mm",{}).get("maxpos", data.get("maxpos", 0)),
            data.get("mm",{}).get("sl", data.get("sl", 0)),
        )
        return data
    except Exception as e:
        logger.debug("_check_config_flag %s: %s", exchange, e)
        return None


def _check_autotrader_flag() -> bool | None:
    """
    Legge /tmp/fs_autotrader.flag scritto dalla dashboard (proxy).
    Ritorna True/False se il flag è presente e recente (<5min), None altrimenti.
    """
    import json as _j
    flag = "/tmp/fs_autotrader.flag"
    try:
        if not os.path.exists(flag):
            return None
        data = _j.loads(open(flag).read())
        if time.time() - data.get("ts", 0) > 300:   # flag scaduto dopo 5 min
            return None
        return bool(data.get("enabled", False))
    except Exception:
        return None


async def trading_job(context):
    """
    Job auto-trading multi-tenant.
    Esegue FundingTrader per ogni utente registrato su Supabase.
    Mantiene retrocompatibilità con il trader legacy single-tenant.
    """
    global _tj_running, TRADING_ENABLED, _funding_trader, _bybit_trader, _last_config_ts

    # Controlla flag dalla dashboard (toggle in tempo reale)
    flag_state = _check_autotrader_flag()
    # Controlla config per tutti gli exchange supportati — notifica se applicata
    _ex_emoji = {"bybit": "🟡", "binance": "🟠", "okx": "🔵"}
    for _ex in ("bybit", "binance", "okx"):
        _cfg = _check_config_flag(_ex)
        if _cfg:
            # Config applicata — notifica già inviata dal proxy in tempo reale
            logger.info("Config applicata exchange=%s size=%s leva=%s",
                        _ex, _cfg.get("mm",{}).get("size","?"), _cfg.get("mm",{}).get("leva","?"))
    if flag_state is not None and flag_state != TRADING_ENABLED:
        if flag_state:
            # Avvia trader se non già attivo
            if _funding_trader is None:
                api_key    = os.getenv("BYBIT_API_KEY", "")
                api_secret = os.getenv("BYBIT_API_SECRET", "")
                if api_key and api_secret:
                    load_config("trader_config.json")
                    from exchanges.bybit import BybitTrader as _BT
                    _bybit_trader   = _BT(api_key=api_key, api_secret=api_secret,
                                          testnet=TRADING_TESTNET, demo=TRADING_DEMO)
                    _funding_trader = FundingTrader(_bybit_trader, lambda *a, **kw: None,
                                                    exchange_name="bybit")
                    _exchange_traders["bybit"] = _funding_trader
                    TRADING_ENABLED = True

                    # Carica config dashboard prima di inizializzare i trader
                    for _ex in ("bybit", "binance"):
                        _check_config_flag(_ex)
                    # Avvia anche Binance se le chiavi sono nel DB
                    await _init_exchange_traders(context.bot)

                    env_label = "🎮 DEMO" if TRADING_DEMO else ("🧪 TESTNET" if TRADING_TESTNET else "🔴 MAINNET")
                    await send_to_owner(context.bot,
                        f"🟢 *Auto-Trader ATTIVATO* · {env_label}\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"💰 Size: {TRADER_CONFIG['size_usdt']} USDT · Leva: {TRADER_CONFIG['leverage']}x\n"
                        f"📊 Max pos: {TRADER_CONFIG['max_positions']} · SL: {TRADER_CONFIG['sl_pct']}%\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Exchange attivi: {', '.join(_exchange_traders.keys())}",
                        parse_mode="Markdown"
                    )
                    logger.info("Auto-trader attivato da dashboard flag su: %s", list(_exchange_traders.keys()))
        else:
            # Disattiva trader
            open_pos = sum(len(ft.positions) for ft in _exchange_traders.values())
            TRADING_ENABLED = False
            _funding_trader = None
            _bybit_trader   = None
            _exchange_traders.clear()
            warning = f"\n⚠️ *{open_pos} open position(s) not closed automatically.*" if open_pos > 0 else ""
            await send_to_owner(context.bot,
                f"🔴 *Auto-Trader disabled* (dashboard)\n"
            f"Exchange: `🟡 Bybit`\n"
                f"Funding alerts still active ✅{warning}")
            logger.info("Auto-trader disattivato da dashboard flag")

    # Monitor posizioni per tutti gli exchange attivi
    if TRADING_ENABLED and _exchange_traders:
        owner_cid = os.getenv("CHAT_ID", CHAT_ID)
        if owner_cid:
            for _exname, _ft in list(_exchange_traders.items()):
                if _ft.positions:
                    try:
                        await _ft.monitor_positions(owner_cid)
                    except Exception as _me:
                        logger.error("monitor_positions %s: %s", _exname, _me)

    if not TRADING_ENABLED:
        return
    if _tj_running:
        logger.warning("⚠️ trading_job: precedente ancora in esecuzione, skip")
        return
    _tj_running = True

    bot: Bot = context.bot

    # Helper send per il manager multi-tenant
    async def _send(chat_id, text):
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error("trading send error %s: %s", chat_id, e)

    try:
        # ── Multi-tenant: usa tickers cachati dal funding_job ──────────────
        tickers = list(_funding_cache_tickers.values()) if _funding_cache_tickers else []
        if tickers and _registry.all_clients():
            await _trading_manager.trading_job(
                registry=_registry,
                tickers=tickers,
                send_fn=_send,
                auto_trading=True,
            )
            return

        # ── Fallback legacy: trader single-tenant ─────────────────────────
        if _funding_trader is None:
            return
        owner_chat_id = os.getenv("CHAT_ID", CHAT_ID)
        if _funding_trader.positions:
            await _funding_trader.monitor_positions(owner_chat_id)
        for symbol in list(_monitoring.keys()):
            try:
                funding_rate = _funding_cache.get(symbol)
                if funding_rate is None:
                    continue
                _funding_trader.update_persistence(symbol, funding_rate)
                await _funding_trader.check_funding_exit(symbol, funding_rate, owner_chat_id)
                ok, reason = await _funding_trader.should_open(symbol, funding_rate)
                if ok:
                    await _funding_trader.open_trade(symbol, funding_rate, owner_chat_id)
                    context.bot_data["trades_opened"] = context.bot_data.get("trades_opened", 0) + 1
            except Exception as e:
                logger.error("trading_job legacy %s: %s", symbol, e)

    except Exception as e:
        logger.error("trading_job outer: %s", e)
    finally:
        _tj_running = False


# ── Comando /autotrader on|off ────────────────────────────────────────────────
async def cmd_autotrader_toggle(update, context):
    """Abilita o disabilita l'auto-trader a runtime. Solo owner."""
    global TRADING_ENABLED, _funding_trader, _bybit_trader

    # Plan gate — Pro required
    from commands import _require_plan
    if not await _require_plan(update, "pro"):
        return

    # Solo owner
    chat_id = str(update.effective_chat.id)
    if chat_id != str(os.getenv("CHAT_ID", CHAT_ID)):
        await update.message.reply_text("⛔ Not authorized.", parse_mode="Markdown")
        return

    args = context.args
    if not args or args[0].lower() not in ("on", "off"):
        status = "🟢 ON" if TRADING_ENABLED else "🔴 OFF"
        env    = "🎮 DEMO" if TRADING_DEMO else ("🧪 TESTNET" if TRADING_TESTNET else "🔴 MAINNET")
        api_key_set = bool(os.getenv("BYBIT_API_KEY", ""))
        exch_label  = "🟡 Bybit" if api_key_set else "no exchange configured"
        await update.message.reply_text(
            f"🤖 *Auto-Trader* — {status}\n"
            f"Exchange: `{exch_label}`\n"
            f"Environment: `{env}`\n\n"
            f"Use `/autotrader on` or `/autotrader off`",
            parse_mode="Markdown"
        )
        return

    action = args[0].lower()

    # ── ON ──────────────────────────────────────────────────────────────────
    if action == "on":
        if TRADING_ENABLED and _funding_trader is not None:
            await update.message.reply_text(
                "✅ *Auto-Trader is already running.*",
                parse_mode="Markdown"
            )
            return

        api_key    = os.getenv("BYBIT_API_KEY", "")
        api_secret = os.getenv("BYBIT_API_SECRET", "")
        if not api_key or not api_secret:
            await update.message.reply_text(
                "⚠️ *Cannot enable Auto-Trader*\n"
                "`BYBIT_API_KEY` or `BYBIT_API_SECRET` missing in `.env`.",
                parse_mode="Markdown"
            )
            return

        load_config("trader_config.json")
        _bybit_trader = BybitTrader(
            api_key=api_key, api_secret=api_secret,
            testnet=TRADING_TESTNET, demo=TRADING_DEMO,
        )

        async def _tg_send_toggle(cid, msg, symbol=None, rate=None):
            try:
                chart_buf = None
                if symbol and rate is not None:
                    try:
                        chart_buf = generate_chart(symbol, rate, exchange=exchange or "bybit")
                    except Exception:
                        pass
                if chart_buf:
                    chart_buf.seek(0)
                    await update.get_bot().send_photo(chat_id=cid, photo=chart_buf,
                                                      caption=msg, parse_mode="Markdown")
                else:
                    await update.get_bot().send_message(chat_id=cid, text=msg, parse_mode="Markdown")
            except Exception as e:
                logger.error("_tg_send_toggle: %s", e)

        _funding_trader = FundingTrader(_bybit_trader, _tg_send_toggle)
        TRADING_ENABLED = True

        env_label = "🎮 DEMO" if TRADING_DEMO else ("🧪 TESTNET" if TRADING_TESTNET else "🔴 LIVE")
        await update.message.reply_text(
            f"🤖 *Auto-Trader activated*\n"
            f"Exchange: `🟡 Bybit`\n"
            f"Environment: `{env_label}`\n"
            f"Size: `{TRADER_CONFIG['size_usdt']} USDT` | "
            f"Leverage: `{TRADER_CONFIG['leverage']}x` | "
            f"Max pos: `{TRADER_CONFIG['max_positions']}`\n"
            f"Config: `trader_config.json` "
            f"{'✅' if os.path.exists('trader_config.json') else '⚠️ not found (using defaults)'}",
            parse_mode="Markdown"
        )

    # ── OFF ─────────────────────────────────────────────────────────────────
    else:
        if not TRADING_ENABLED or _funding_trader is None:
            await update.message.reply_text(
                "🔴 *Auto-Trader is already disabled.*",
                parse_mode="Markdown"
            )
            return

        open_pos = len(_funding_trader.positions) if _funding_trader else 0

        if open_pos == 0:
            # Nessuna posizione aperta — disattiva direttamente
            TRADING_ENABLED = False
            _funding_trader = None
            _bybit_trader   = None
            await update.message.reply_text(
                "🔴 *Auto-Trader disabled*\n"
                "Exchange: `🟡 Bybit`\n"
                "No new trades will be opened.\n"
                "Funding alerts are still active ✅",
                parse_mode="Markdown"
            )
        else:
            # Posizioni aperte — chiedi conferma con InlineKeyboard
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        f"❌ Close all {open_pos} position(s) & disable",
                        callback_data="autotrader_off_close_all"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "⏸ Disable only (keep positions open)",
                        callback_data="autotrader_off_only"
                    ),
                ],
                [
                    InlineKeyboardButton("🚫 Cancel", callback_data="autotrader_off_cancel"),
                ],
            ])
            await update.message.reply_text(
                f"⚠️ *Auto-Trader — Confirm disable*\n\n"
                f"You have *{open_pos} open position(s)* on 🟡 Bybit.\n"
                f"What do you want to do?",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )


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

    env_label = "🎮 DEMO" if TRADING_DEMO else ("🧪 TESTNET" if TRADING_TESTNET else "🔴 LIVE")
    msg += f"\n━━━━━━━━━━━━━━━━━━\n🟡 Exchange: `Bybit` · ⚡ Environment: `{env_label}`"

    await update.message.reply_text(msg, parse_mode="Markdown")


# ── Comando /posizioni_trader ─────────────────────────────────────────────────
async def cmd_posizioni_trader(update, context):
    """Mostra le posizioni aperte dall'auto-trader con dettagli completi."""
    if _funding_trader is None or not _funding_trader.positions:
        await update.message.reply_text(
            "📂 No open positions from auto-trader.",
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
async def referral_payout_job(context):
    """Job mensile il 1° del mese: processa i payout referral."""
    bot: Bot = context.bot
    try:
        from referral import process_monthly_payouts
        count = await process_monthly_payouts(bot)
        logger.info("Referral payout job completato: %d payout processati", count)
        if count > 0:
            await send_to_owner(bot, f"💸 *Referral Payout* — {count} payout processati questo mese.")
    except Exception as e:
        logger.error("referral_payout_job: %s", e)


async def plan_expiry_job(context):
    """
    Job giornaliero alle 09:00 IT:
    - Notifica utenti con piano in scadenza tra 3 giorni
    - Downgrade automatico a Free per piani scaduti
    """
    bot: Bot = context.bot
    from datetime import datetime, timedelta, timezone
    from db.supabase_client import get_client as _gc, update_user_plan

    try:
        db  = _gc()
        now = datetime.now(timezone.utc)

        # Recupera tutti gli utenti con piano Pro/Elite
        res = db.table("users").select(
            "id,chat_id,telegram_handle,plan,plan_expires_at,billing_type"
        ).in_("plan", ["pro", "elite"]).execute()

        users = res.data or []
        logger.info("plan_expiry_job: %d utenti con piano attivo", len(users))

        for u in users:
            try:
                exp_str = u.get("plan_expires_at")
                if not exp_str:
                    continue

                exp_dt  = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                chat_id = u.get("chat_id")
                plan    = u.get("plan", "free")
                billing = u.get("billing_type", "oneshot")
                days_left = (exp_dt - now).days

                # ── Scaduto → downgrade a Free ────────────────────────────────
                if now > exp_dt:
                    await update_user_plan(
                        user_id=u["id"],
                        plan="free",
                        billing_type=None,
                        expires_at=None,
                    )
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=(
                                f"⚠️ *Your {plan.capitalize()} plan has expired.*\n\n"
                                "You've been moved to the Free plan.\n"
                                "Use /upgrade to renew and keep all your features."
                            ),
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        logger.warning("plan_expiry notify expired %s: %s", chat_id, e)
                    logger.info("Piano scaduto → Free: chat_id=%s", chat_id)
                    continue

                # ── Scade tra 3 giorni → notifica ─────────────────────────────
                if days_left == 3:
                    billing_lbl = "🔄 recurring" if billing == "recurring" else "1️⃣ one-shot"
                    prices = {"pro": {"recurring": 15, "oneshot": 20},
                              "elite": {"recurring": 40, "oneshot": 50}}
                    price = prices.get(plan, {}).get(billing or "oneshot", 0)
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=(
                                f"⏰ *Your {plan.capitalize()} plan expires in 3 days.*\n\n"
                                f"📅 Expiry: `{exp_dt.strftime('%d/%m/%Y')}`\n"
                                f"💳 Billing: {billing_lbl}\n\n"
                                f"Renew now for ${price} to keep auto-trading and all features.\n\n"
                                "👉 Use /upgrade to renew."
                            ),
                            parse_mode="Markdown",
                        )
                        logger.info("Piano in scadenza notificato: chat_id=%s days_left=%d", chat_id, days_left)
                    except Exception as e:
                        logger.warning("plan_expiry notify 3days %s: %s", chat_id, e)

                # ── Scade domani → ultima notifica ────────────────────────────
                elif days_left == 1:
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=(
                                f"🚨 *Your {plan.capitalize()} plan expires tomorrow!*\n\n"
                                "Renew now to avoid losing access to auto-trading.\n\n"
                                "👉 /upgrade"
                            ),
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        logger.warning("plan_expiry notify 1day %s: %s", chat_id, e)

            except Exception as e:
                logger.error("plan_expiry_job user %s: %s", u.get("chat_id"), e)

    except Exception as e:
        logger.error("plan_expiry_job: %s", e)


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

    # Setup NOWPayments subscription plans (una volta all'avvio)
    try:
        from payments import setup_subscription_plans
        plans = setup_subscription_plans()
        if plans:
            logger.info("NOWPayments subscription plans: %s", plans)
        else:
            logger.warning("NOWPayments subscription plans non creati — controlla NOWPAY_API_KEY")
    except Exception as e:
        logger.warning("setup_subscription_plans: %s", e)

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

    # ── SaaS: carica tutti gli utenti dal registry Supabase ──────────────────
    try:
        n = await _registry.refresh()
        logger.info("Registry SaaS: %d client attivi", n)
    except Exception as e:
        logger.warning("Registry SaaS non disponibile: %s", e)

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
                            chart_buf = generate_chart(symbol, rate, exchange="bybit")
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

            _funding_trader = FundingTrader(_bybit_trader, _tg_send,
                                            exchange_name="bybit")
            _exchange_traders["bybit"] = _funding_trader

            env_label = "🎮 DEMO" if TRADING_DEMO else ("🧪 TESTNET" if TRADING_TESTNET else "🔴 MAINNET")
            # Carica config dashboard se disponibile
            for _ex in ("bybit", "binance"):
                _check_config_flag(_ex)
            # Inizializza traders per tutti gli exchange
            await _init_exchange_traders(app.bot)

            logger.info(
                "🤖 Auto-trader attivo — %s | size=%.0f USDT | leva=%dx | maxpos=%d",
                env_label,
                TRADER_CONFIG["size_usdt"],
                TRADER_CONFIG["leverage"],
                TRADER_CONFIG["max_positions"],
            )

            # Notifica owner al boot — con dettagli completi
            from db.supabase_client import get_client as _bc2
            try:
                _db2 = _bc2()
                _rows = _db2.table("exchange_credentials").select("exchange,environment").eq("is_active", True).execute()
                _exch_list = [f"{'🟡' if r['exchange']=='bybit' else '🟠' if r['exchange']=='binance' else '🔵'} {r['exchange'].capitalize()} ({'Demo' if r['environment']=='demo' else 'Live'})"
                              for r in (_rows.data or [])]
                _exch_str = " · ".join(_exch_list) if _exch_list else "nessuno"
            except Exception:
                _exch_str = "—"

            # Costruisci messaggio con config specifica per ogni exchange
            import json as _bj, os as _bos
            _bex_em = {"bybit": "🟡", "binance": "🟠", "okx": "🔵", "hyperliquid": "🟣"}
            _blines = []
            for _br in (_rows.data or []):
                _bex  = _br["exchange"]
                _benv = "Demo" if _br["environment"] == "demo" else "Live"
                _bem  = _bex_em.get(_bex, "⚡")
                _bcf  = "/tmp/fs_config_" + _bex + ".json"
                _bsz  = str(TRADER_CONFIG["size_usdt"])
                _blv  = str(TRADER_CONFIG["leverage"])
                _bsl  = str(TRADER_CONFIG["sl_pct"])
                _bmp  = str(TRADER_CONFIG["max_positions"])
                try:
                    if _bos.path.exists(_bcf):
                        _bmm = _bj.loads(open(_bcf).read()).get("mm", {})
                        _bsz = str(_bmm.get("size", _bsz))
                        _blv = str(_bmm.get("leva", _blv))
                        _bsl = str(_bmm.get("sl",   _bsl))
                        _bmp = str(_bmm.get("maxpos", _bmp))
                except Exception:
                    pass
                _blines.append(
                    _bem + " *" + _bex.capitalize() + "* (" + _benv + ")"
                    + " — " + _bsz + " USDT · " + _blv + "x · SL " + _bsl + "% · Max " + _bmp
                )
            _bdetail = "\n".join(_blines) if _blines else "nessuno"
            _bsep = "━━━━━━━━━━━━━━━━━━"
            _bmsg = (
                "🚀 *FundShot Bot Online*\n" + _bsep + "\n"
                "🤖 Auto-Trader: 🟢 ATTIVO\n" + _bsep + "\n"
                "🔑 Exchange attivi:\n" + _bdetail + "\n" + _bsep + "\n"
                "📡 Alert attivi · MM ✅ · Guardian ✅"
            )
            await send_to_owner(app.bot, _bmsg)
        else:
            logger.warning("AUTO_TRADING=true but API keys not configured.")
            await send_to_owner(
                app.bot,
                "⚠️ *Auto-Trader not started*\n"
                "`AUTO_TRADING=true` but API keys are missing.\n"
                "Set `BYBIT_API_KEY` and `BYBIT_API_SECRET` in `.env` and restart."
            )
    else:
        logger.info("Auto-trading DISABLED (AUTO_TRADING=false)")
        # Leggi exchange configurati anche senza autotrader
        try:
            from db.supabase_client import get_client as _bc3
            _db3 = _bc3()
            _rows3 = _db3.table("exchange_credentials").select("exchange,environment").eq("is_active", True).execute()
            _exch_list3 = [f"{'🟡' if r['exchange']=='bybit' else '🟠' if r['exchange']=='binance' else '🔵'} {r['exchange'].capitalize()} ({'Demo' if r['environment']=='demo' else 'Live'})"
                           for r in (_rows3.data or [])]
            _exch_str3 = " · ".join(_exch_list3) if _exch_list3 else "nessuno"
        except Exception:
            _exch_str3 = "—"
        await send_to_owner(
            app.bot,
            f"🚀 *FundShot Bot Online*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🤖 Auto-Trader: `🔴 OFF`\n"
            f"📡 Alert attivi ✅ · Guardian ✅\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔑 Exchange configurati:\n{_exch_str3}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Usa /autotrader on per attivare il trading"
        )

    # 5. Registra comandi e Menu Button Telegram
    await _setup_bot_menu(app.bot)


# ── Setup Menu Button + comandi Telegram ──────────────────────────────────────
async def _setup_bot_menu(bot):
    """Registra i comandi e attiva il Menu Button (☰) accanto alla barra di testo."""
    from telegram import BotCommand, MenuButtonCommands

    bot_commands = [
        # ── Funding Rate ──────────────────────────────────────────────────────
        BotCommand("top10",      "🔥 Top 10 SHORT + LONG funding rates"),
        BotCommand("storico",    "📅 Funding history — /storico SYM [7g]"),
        BotCommand("backtest",   "🧪 Simulate 30-day P&L ⚡ Pro"),
        # ── Account ───────────────────────────────────────────────────────────
        BotCommand("saldo",      "💼 Wallet balance per exchange"),
        BotCommand("posizioni",  "📂 Open positions with PnL"),
        # ── Auto-Trading ──────────────────────────────────────────────────────
        BotCommand("autotrader", "🤖 Toggle auto-trader on/off ⚡ Pro"),
        # ── Watchlist ─────────────────────────────────────────────────────────
        BotCommand("watchlist",  "👁 Full watchlist status"),
        BotCommand("watch",      "➕ Add symbols — /watch BTC ETH SOL"),
        BotCommand("unwatch",    "➖ Remove symbols — /unwatch all"),
        BotCommand("mute",       "🔇 Mute alerts for a symbol"),
        BotCommand("unmute",     "🔔 Reactivate alerts for a symbol"),
        # ── Subscription ──────────────────────────────────────────────────────
        BotCommand("plan",       "💳 Your plan, expiry and billing"),
        BotCommand("upgrade",    "⚡ Upgrade to Pro or Elite"),
        BotCommand("referral",   "🔗 Your referral link + earnings"),
        BotCommand("setwallet",  "💸 Set USDT wallet for payouts"),
        # ── Admin (owner only) ────────────────────────────────────────────────
        BotCommand("addinf",     "👑 Make a user Influencer (admin only)"),
        BotCommand("payoutlist", "💸 List pending referral payouts (admin only)"),
        BotCommand("clearpayouts","✅ Clear payouts after sending (admin only)"),
        BotCommand("alertfilter","🔔 Filter alerts by level/cooldown"),
        # ── Settings ──────────────────────────────────────────────────────────
        BotCommand("start",      "🚀 Configure exchange API keys"),
        BotCommand("deletekeys", "🗑 Remove your API keys"),
        BotCommand("help",       "📋 All commands"),
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

    # Scheduled: plan expiry check alle 09:00 IT
    app.job_queue.run_daily(
        plan_expiry_job,
        time=dt_time(hour=9, minute=0, second=0, tzinfo=TZ_IT),
        name="plan_expiry",
    )
    logger.info("Plan expiry job schedulato alle 09:00 IT")

    # Scheduled: referral payout il 1° di ogni mese alle 10:00 IT
    from telegram.ext import CommandHandler
    app.job_queue.run_monthly(
        referral_payout_job,
        when=dt_time(hour=10, minute=0, second=0, tzinfo=TZ_IT),
        day=1,
        name="referral_payout",
    )
    logger.info("Referral payout job schedulato il 1° del mese alle 10:00 IT")

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

    # Registra wizard onboarding SaaS — DEVE essere prima di commands.register()
    app.add_handler(onboarding.build_onboarding_handler())

    # Registra wizard upgrade piani
    app.add_handler(commands.build_upgrade_handler())

    # Registra handler comandi (da commands.py)
    commands.inject_bot_commands(cmd_stats, cmd_posizioni_trader)
    commands.register(app)

    # Registra handler comandi trading inline
    from telegram.ext import CommandHandler
    app.add_handler(CommandHandler("stats",            cmd_stats))
    app.add_handler(CommandHandler("test_oi",          cmd_test_oi))
    app.add_handler(CommandHandler("posizioni_trader", cmd_posizioni_trader))
    app.add_handler(CommandHandler("autotrader",       cmd_autotrader_toggle))
    app.add_handler(CommandHandler("plan",             commands.cmd_plan))

    # ── Inline keyboard callbacks ────────────────────────────────────────────
    from telegram.ext import CallbackQueryHandler as CQH

    async def _autotrader_off_callback(update, context):
        global TRADING_ENABLED, _funding_trader, _bybit_trader
        q = update.callback_query
        await q.answer()

        if q.data == "autotrader_off_cancel":
            await q.edit_message_text("✅ *Cancelled.* Auto-Trader is still running.", parse_mode="Markdown")
            return

        if q.data == "autotrader_off_close_all":
            open_pos = len(_funding_trader.positions) if _funding_trader else 0
            # Chiudi tutte le posizioni
            closed = 0
            if _funding_trader and _bybit_trader:
                for sym in list(_funding_trader.positions.keys()):
                    try:
                        await _bybit_trader.close_position(sym)
                        closed += 1
                    except Exception as e:
                        logger.error("close_position %s: %s", sym, e)
            TRADING_ENABLED = False
            _funding_trader = None
            _bybit_trader   = None
            await q.edit_message_text(
                f"🔴 *Auto-Trader disabled*\n"
                f"Exchange: `🟡 Bybit`\n"
                f"Closed: *{closed}/{open_pos}* positions\n"
                f"Funding alerts are still active ✅",
                parse_mode="Markdown"
            )

        elif q.data == "autotrader_off_only":
            open_pos = sum(len(ft.positions) for ft in _exchange_traders.values())
            TRADING_ENABLED = False
            _funding_trader = None
            _bybit_trader   = None
            _exchange_traders.clear()
            await q.edit_message_text(
                f"🔴 *Auto-Trader disabled*\n"
                f"Exchange: `🟡 Bybit`\n"
                f"⚠️ *{open_pos} position(s) left open* — manage manually on exchange.\n"
                f"Funding alerts are still active ✅",
                parse_mode="Markdown"
            )

    app.add_handler(CQH(_autotrader_off_callback, pattern="^autotrader_off_"))

    logger.info(
        "🚀 FundShot Bot avviato — interval=%ds | soglie=%s | trading=%s",
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
