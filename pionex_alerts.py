"""
pionex_alerts.py — FundShot
Monitora i funding rate di Pionex e manda alert a un gruppo Telegram.

Setup:
  pip install aiohttp python-telegram-bot
  
  Variabili d'ambiente:
    BOT_TOKEN          — token del bot Telegram (stesso di FundShot o uno dedicato)
    PIONEX_GROUP_ID    — chat_id del gruppo Telegram (-100xxxxxxxxx)
  
  Esecuzione:
    python3 pionex_alerts.py
  
  Come servizio systemd:
    ExecStart=/root/fundshot/venv/bin/python3 /root/fundshot/pionex_alerts.py
"""

import asyncio
import logging
import os
import time
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from telegram import Bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("pionex_alerts")

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.getenv("BOT_TOKEN", "")
GROUP_ID        = os.getenv("PIONEX_GROUP_ID", "")   # es. -1001234567890
INTERVAL_SEC    = 120          # controlla ogni 2 minuti
TZ_IT           = ZoneInfo("Europe/Rome")

# Soglie alert (% per intervallo)
THRESHOLDS = {
    "jackpot": 2.5,
    "hard":    2.0,
    "extreme": 1.5,
    "high":    1.0,
    "soft":    0.5,
}

LEVEL_EMOJI = {
    "jackpot": "💎 JACKPOT",
    "hard":    "🔴 HARD",
    "extreme": "🔥 EXTREME",
    "high":    "🚨 HIGH",
    "soft":    "📊 SOFT",
}

# Cooldown per evitare alert ripetuti (secondi)
COOLDOWNS = {
    "jackpot": 900,   # 15 min
    "hard":    900,
    "extreme": 1800,  # 30 min
    "high":    3600,  # 60 min
    "soft":    3600,
}

# Stato in memoria: {symbol: {"level": str, "last_sent": float}}
_state: dict[str, dict] = {}
_state_file = "/tmp/pionex_alert_state.json"

# ── State persistence ─────────────────────────────────────────────────────────
def save_state():
    try:
        open(_state_file, "w").write(json.dumps(_state))
    except Exception as e:
        logger.warning("save_state: %s", e)

def load_state():
    global _state
    try:
        raw = open(_state_file).read().strip()
        if raw:
            loaded = json.loads(raw)
            # Mantieni solo stati recenti (< 8 ore)
            cutoff = time.time() - 28800
            _state = {k: v for k, v in loaded.items() if v.get("last_sent", 0) > cutoff}
    except Exception:
        pass

# ── Pionex API ────────────────────────────────────────────────────────────────
PIONEX_BASE = "https://api.pionex.com"

async def fetch_pionex_tickers(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch tutti i ticker perpetual da Pionex (endpoint pubblico)."""
    try:
        # Endpoint market tickers — pubblico, no auth
        async with session.get(
            f"{PIONEX_BASE}/api/v1/market/tickers",
            params={"type": "PERP_USDT"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status != 200:
                logger.warning("Pionex tickers HTTP %d", r.status)
                return []
            data = await r.json()
            if not data.get("result"):
                logger.warning("Pionex tickers error: %s", data)
                return []
            return data.get("data", {}).get("tickers", [])
    except Exception as e:
        logger.warning("fetch_pionex_tickers: %s", e)
        return []

async def fetch_pionex_funding_rates(session: aiohttp.ClientSession) -> list[dict]:
    """
    Fetch funding rate correnti per tutti i perpetual Pionex.
    Prova diversi endpoint — Pionex non ha un endpoint dedicato pubblico documentato,
    ma il funding rate è incluso nei ticker perpetual.
    """
    tickers = await fetch_pionex_tickers(session)
    
    results = []
    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        
        funding_rate = t.get("fundingRate") or t.get("funding_rate") or t.get("nextFundingRate")
        if funding_rate is None:
            continue
        
        try:
            rate_pct = float(funding_rate) * 100
            results.append({
                "symbol":      symbol,
                "rate_pct":    rate_pct,
                "mark_price":  float(t.get("close", 0) or 0),
                "next_funding": t.get("nextFundingTime", ""),
            })
        except (ValueError, TypeError):
            continue
    
    return results

# ── Classificazione livello ───────────────────────────────────────────────────
def classify(rate_pct: float) -> str | None:
    abs_rate = abs(rate_pct)
    for level, threshold in THRESHOLDS.items():
        if abs_rate >= threshold:
            return level
    return None

def should_send(symbol: str, level: str) -> bool:
    now = time.time()
    st = _state.get(symbol, {})
    last_level = st.get("level", "")
    last_sent  = st.get("last_sent", 0)
    cooldown   = COOLDOWNS.get(level, 1800)
    
    # Invia se: nuovo livello più alto, o cooldown scaduto
    if level != last_level:
        return True
    if now - last_sent >= cooldown:
        return True
    return False

# ── Formato alert ─────────────────────────────────────────────────────────────
def format_alert(symbol: str, rate_pct: float, level: str, mark_price: float) -> str:
    direction = "SHORT 📉" if rate_pct > 0 else "LONG 📈"
    lvl_label = LEVEL_EMOJI.get(level, level.upper())
    now_str   = datetime.now(TZ_IT).strftime("%H:%M")
    
    return (
        f"*{lvl_label} — {direction}*\n\n"
        f"📌 `{symbol}`  ·  🟣 Pionex\n"
        f"📊 `{rate_pct:+.4f}%` funding rate\n"
        f"💵 Mark: `${mark_price:,.4f}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {now_str} IT  ·  #PionexAlert"
    )

# ── Loop principale ───────────────────────────────────────────────────────────
async def main_loop():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN non impostato — exit")
        return
    if not GROUP_ID:
        logger.error("PIONEX_GROUP_ID non impostato — exit")
        return

    bot = Bot(token=BOT_TOKEN)
    load_state()
    logger.info("Pionex alerts avviati → gruppo %s", GROUP_ID)

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                tickers = await fetch_pionex_funding_rates(session)
                
                if not tickers:
                    logger.warning("Nessun ticker Pionex ricevuto — API potrebbe essere giù")
                else:
                    logger.info("Pionex: %d tickers ricevuti", len(tickers))
                
                alerts_sent = 0
                for t in tickers:
                    symbol    = t["symbol"]
                    rate_pct  = t["rate_pct"]
                    mark      = t["mark_price"]
                    level     = classify(rate_pct)
                    
                    if not level:
                        # Rate normale — resetta stato
                        if symbol in _state:
                            del _state[symbol]
                        continue
                    
                    if not should_send(symbol, level):
                        continue
                    
                    # Invia alert al gruppo
                    msg = format_alert(symbol, rate_pct, level, mark)
                    try:
                        await bot.send_message(
                            chat_id=int(GROUP_ID),
                            text=msg,
                            parse_mode="Markdown",
                        )
                        _state[symbol] = {"level": level, "last_sent": time.time()}
                        alerts_sent += 1
                        logger.info("Alert inviato: %s %s %+.4f%%", level, symbol, rate_pct)
                        await asyncio.sleep(0.3)  # anti-flood
                    except Exception as e:
                        logger.error("Telegram send error %s: %s", symbol, e)
                
                if alerts_sent:
                    save_state()
                    logger.info("Ciclo completato: %d alert inviati", alerts_sent)
                
            except Exception as e:
                logger.error("main_loop error: %s", e)
            
            await asyncio.sleep(INTERVAL_SEC)

if __name__ == "__main__":
    asyncio.run(main_loop())
