#!/usr/bin/env python3
"""
generate_track_record.py — FundShot
Genera il track record pubblico basato su backtest 60 giorni Bybit.
Output: /tmp/fs_track_record.json (letto dal proxy e servito alla landing)

Eseguito:
  - Manualmente: python3 generate_track_record.py
  - Da cron: ogni 24h (aggiorna il record)
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import bybit_client as bc
import alert_logic as al
from backtester import run_backtest, TAKER_FEE, SLIPPAGE

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

TZ_IT   = ZoneInfo("Europe/Rome")
DAYS    = 60          # backtest 60 giorni
OUTPUT  = "/tmp/fs_track_record.json"
TOP_N   = 30          # top simboli per volume/funding

# Config simulata (allineata ai default del bot)
SIZE_USDT  = 100.0
LEVERAGE   = 5
MAX_POS    = 4


async def fetch_60d(symbol: str) -> list[dict]:
    """Fetch funding history 60 giorni con paginazione."""
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - DAYS * 24 * 3600 * 1000
    all_entries = []
    cursor = ""
    for page in range(20):  # max 20 pagine = 4000 record
        try:
            kwargs = {
                "category":  "linear",
                "symbol":    symbol,
                "startTime": str(start_ms),
                "endTime":   str(now_ms),
                "limit":     200,
            }
            if cursor:
                kwargs["cursor"] = cursor
            res = await bc._run(bc.get_session().get_funding_rate_history, **kwargs)
            if res.get("retCode") != 0:
                break
            entries = res["result"].get("list", [])
            all_entries.extend(entries)
            cursor = res["result"].get("nextPageCursor", "")
            if not cursor or not entries:
                break
        except Exception as e:
            logger.error("fetch_60d %s: %s", symbol, e)
            break
    all_entries.sort(key=lambda x: int(x.get("fundingRateTimestamp", 0)))
    return all_entries


async def get_top_symbols() -> list[str]:
    """Prendi i top N simboli per funding rate assoluto medio o volume."""
    try:
        tickers = await bc.get_funding_tickers()
        # Ordina per |funding rate| decrescente e prendi top N
        sorted_t = sorted(tickers, key=lambda t: abs(float(t.get("fundingRate", 0))), reverse=True)
        return [t["symbol"] for t in sorted_t[:TOP_N] if t.get("symbol", "").endswith("USDT")]
    except Exception as e:
        logger.error("get_top_symbols: %s", e)
        # Fallback: simboli più noti
        return ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
                "DOGEUSDT","AVAXUSDT","ADAUSDT","DOTUSDT","LINKUSDT"]


async def main():
    logger.info("Avvio generazione track record 60gg...")

    symbols = await get_top_symbols()
    logger.info("Simboli selezionati: %d", len(symbols))

    all_trades  = []
    all_results = []
    sem = asyncio.Semaphore(5)

    async def process(sym):
        async with sem:
            logger.info("Fetching %s...", sym)
            entries = await fetch_60d(sym)
            if not entries:
                return None
            result = run_backtest(sym, entries)
            return result

    tasks = [asyncio.create_task(process(s)) for s in symbols]
    for coro in asyncio.as_completed(tasks):
        r = await coro
        if r and r.trades:
            all_results.append(r)
            all_trades.extend(r.trades)

    if not all_trades:
        logger.warning("Nessun trade trovato")
        return

    # ── Calcola statistiche aggregate ────────────────────────────────────
    total_trades = len(all_trades)
    wins         = [t for t in all_trades if t.is_win]
    losses       = [t for t in all_trades if not t.is_win]
    win_rate     = len(wins) / total_trades * 100 if total_trades else 0

    # P&L cumulativo simulando SIZE_USDT per trade
    total_pnl_usdt = sum(t.net_pnl * SIZE_USDT * LEVERAGE for t in all_trades)
    avg_pnl_pct    = sum(t.net_pnl_pct for t in all_trades) / total_trades if total_trades else 0

    # Top simboli per P&L
    sym_pnl = {}
    for r in all_results:
        sym_pnl[r.symbol] = {
            "symbol":      r.symbol,
            "trades":      len(r.trades),
            "win_rate":    round(r.win_rate, 1),
            "total_pnl_pct": round(r.total_pnl_pct, 3),
            "avg_pnl_pct": round(r.avg_pnl_pct, 3),
        }

    top_symbols = sorted(sym_pnl.values(), key=lambda x: x["total_pnl_pct"], reverse=True)[:10]

    # Monthly breakdown (ultimi 2 mesi)
    monthly = {}
    for t in all_trades:
        if t.exit_ts:
            m = datetime.fromtimestamp(t.exit_ts/1000, tz=timezone.utc).strftime("%Y-%m")
            if m not in monthly:
                monthly[m] = {"trades": 0, "wins": 0, "pnl_pct": 0.0}
            monthly[m]["trades"] += 1
            if t.is_win:
                monthly[m]["wins"] += 1
            monthly[m]["pnl_pct"] = round(monthly[m]["pnl_pct"] + t.net_pnl_pct, 4)

    # Equity curve (cumulativo per trade ordinato per data)
    sorted_trades = sorted(all_trades, key=lambda t: t.entry_ts)
    equity = []
    cum = 0.0
    for t in sorted_trades:
        cum += t.net_pnl * SIZE_USDT * LEVERAGE
        equity.append({
            "ts": t.entry_ts,
            "eq": round(cum, 2),
        })
    # Campiona max 100 punti
    if len(equity) > 100:
        step = len(equity) // 100
        equity = equity[::step]

    # ── Output JSON ──────────────────────────────────────────────────────
    record = {
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "exchange":        "Bybit",
        "days":            DAYS,
        "symbols_analyzed": len(symbols),
        "config": {
            "size_usdt":  SIZE_USDT,
            "leverage":   LEVERAGE,
            "fee_pct":    round((TAKER_FEE + SLIPPAGE) * 100 * 2, 4),
            "strategy":   "SHORT on HIGH+ funding, LONG on HIGH- funding",
        },
        "summary": {
            "total_trades":    total_trades,
            "wins":            len(wins),
            "losses":          len(losses),
            "win_rate_pct":    round(win_rate, 1),
            "total_pnl_usdt":  round(total_pnl_usdt, 2),
            "avg_pnl_pct":     round(avg_pnl_pct, 4),
            "best_trade_pct":  round(max(t.net_pnl_pct for t in all_trades), 3),
            "worst_trade_pct": round(min(t.net_pnl_pct for t in all_trades), 3),
        },
        "monthly":         monthly,
        "top_symbols":     top_symbols,
        "equity_curve":    equity,
    }

    with open(OUTPUT, "w") as f:
        json.dump(record, f, indent=2)

    logger.info("Track record salvato in %s", OUTPUT)
    logger.info("Totale: %d trades, win rate %.1f%%, PnL %.2f USDT",
                total_trades, win_rate, total_pnl_usdt)


if __name__ == "__main__":
    asyncio.run(main())
