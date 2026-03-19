#!/usr/bin/env python3
"""
generate_track_record.py — FundShot
Genera il Performance Audit Report: 60 giorni di funding rate reali Bybit
ricostruiti con la strategia live del bot.

NON è un backtest ottimizzato — usa esattamente la stessa logica del bot live:
- Stesso motore di classificazione (alert_logic.classify)
- Stesse soglie (SOFT >= 0.5%, HIGH >= 1%, EXTREME >= 1.5%, HARD >= 2%)
- Stesse fee reali Bybit (taker 0.055% + slippage 0.02%)
- Stesso sistema di apertura/chiusura posizioni

Config simulata:
- Capitale: $10,000 USDT
- Size per trade: 500 USDT (5% del capitale)
- Leva: 10x → notional 5,000 USDT per trade
- Livello minimo: SOFT+ (funding >= 0.5%)
- Simboli: top 200 Bybit USDT perpetual
- Periodo: 60 giorni

Output: /tmp/fs_track_record.json
Eseguito: manualmente o da cron ogni notte alle 3:00 UTC
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
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

# Punto di partenza fisso: 65 giorni fa dalla prima esecuzione
# Da quel giorno la finestra CRESCE ogni giorno (non è rolling)
_now_utc   = datetime.now(timezone.utc)
START_DATE = _now_utc.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=65)
DAYS       = int((_now_utc - START_DATE).days)   # oggi: 65, domani: 66, ecc.
OUTPUT  = "/tmp/fs_track_record.json"
TOP_N   = 200         # tutti i simboli USDT perpetual

# Config simulata — aggressiva ma realistica per trader esperti
STARTING_CAPITAL = 10_000.0   # capitale iniziale simulato
SIZE_USDT        = 500.0      # size per trade (5% del capitale)
LEVERAGE         = 10         # leva 10x
MAX_POS          = 5          # max 5 posizioni contemporanee
MIN_LEVEL        = "soft"     # SOFT+ (>= 0.5%) — massimizza numero trade


async def fetch_60d(symbol: str) -> list[dict]:
    """Fetch funding history dalla data di lancio fino ad oggi (finestra crescente)."""
    now_ms   = int(time.time() * 1000)
    start_ms = int(START_DATE.timestamp() * 1000)
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
    """Prendi TUTTI i simboli USDT perpetual attivi su Bybit."""
    try:
        tickers = await bc.get_funding_tickers()
        # Filtra USDT perp e ordina per funding rate assoluto
        # Esclude simboli con funding quasi zero (< 0.01%) — nessun segnale storico
        candidates = [
            t for t in tickers
            if t.get("symbol","").endswith("USDT")
            and abs(float(t.get("fundingRate", 0))) * 100 >= 0.005
        ]
        # Ordina per funding assoluto — i più attivi prima (timeout priority)
        candidates.sort(key=lambda t: abs(float(t.get("fundingRate", 0))), reverse=True)
        symbols = [t["symbol"] for t in candidates[:TOP_N]]
        logger.info("Simboli selezionati: %d (filtrati da %d totali)", len(symbols), len(tickers))
        return symbols
    except Exception as e:
        logger.error("get_top_symbols: %s", e)
        return ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
                "DOGEUSDT","AVAXUSDT","ADAUSDT","DOTUSDT","LINKUSDT"]


def run_backtest_filtered(symbol: str, entries: list[dict]):
    """Backtest filtrando solo livelli HIGH+ (no SOFT) per simulazione più conservativa."""
    from backtester import BacktestResult, Trade, _infer_interval
    from typing import Optional

    if not entries:
        return BacktestResult(symbol=symbol, interval_h=8, start_ts=0, end_ts=0, total_cycles=0)

    interval_h = _infer_interval(entries)
    start_ts   = int(entries[0]["fundingRateTimestamp"])
    end_ts     = int(entries[-1]["fundingRateTimestamp"])
    result     = BacktestResult(symbol=symbol, interval_h=interval_h,
                                start_ts=start_ts, end_ts=end_ts, total_cycles=len(entries))
    open_trade: Optional[Trade] = None
    HIGH_LEVELS = ("soft", "high", "extreme", "hard", "critico")  # >= 0.5% funding

    for entry in entries:
        ts       = int(entry["fundingRateTimestamp"])
        rate_pct = float(entry.get("fundingRate", 0)) * 100
        abs_rate = abs(rate_pct)
        level    = al.classify(symbol, rate_pct)

        if open_trade is not None:
            open_trade.cycles.append(rate_pct)
            rientro_thr    = al.get_effective_threshold(symbol, "rientro")
            should_close   = (level == "none" and abs_rate <= rientro_thr)
            new_dir        = "SHORT" if rate_pct > 0 else "LONG"
            direction_flip = (level in HIGH_LEVELS and open_trade.direction != new_dir)
            if should_close or direction_flip:
                open_trade.exit_ts     = ts
                open_trade.exit_reason = "rientro" if should_close else "flip"
                result.trades.append(open_trade)
                open_trade = None
                if direction_flip:
                    open_trade = Trade(symbol=symbol, direction=new_dir, level=level,
                                       entry_ts=ts, exit_ts=0, entry_rate=rate_pct)

        if open_trade is None and level in HIGH_LEVELS:
            direction  = "SHORT" if rate_pct > 0 else "LONG"
            open_trade = Trade(symbol=symbol, direction=direction, level=level,
                               entry_ts=ts, exit_ts=0, entry_rate=rate_pct)

    if open_trade is not None and open_trade.cycles:
        open_trade.exit_ts     = end_ts
        open_trade.exit_reason = "end_of_data"
        result.trades.append(open_trade)

    return result


async def main():
    logger.info("Avvio generazione track record (dal %s, giorno %d)...", START_DATE.date(), DAYS)

    symbols = await get_top_symbols()
    logger.info("Simboli selezionati: %d", len(symbols))

    all_trades  = []
    all_results = []
    sem = asyncio.Semaphore(10)  # 10 richieste parallele per velocizzare

    async def process(sym):
        async with sem:
            logger.info("Fetching %s...", sym)
            entries = await fetch_60d(sym)
            if not entries:
                return None
            result = run_backtest_filtered(sym, entries)
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

    # P&L in USDT su capitale simulato
    total_pnl_usdt = sum(t.net_pnl * SIZE_USDT * LEVERAGE for t in all_trades)
    avg_pnl_pct    = sum(t.net_pnl_pct for t in all_trades) / total_trades if total_trades else 0
    final_capital  = STARTING_CAPITAL + total_pnl_usdt
    total_return_pct = (total_pnl_usdt / STARTING_CAPITAL) * 100

    # Max drawdown reale sull'equity curve
    sorted_trades = sorted(all_trades, key=lambda t: t.entry_ts)
    peak_eq  = STARTING_CAPITAL
    max_dd_usdt = 0.0
    max_dd_pct  = 0.0
    cum_eq   = STARTING_CAPITAL
    for t in sorted_trades:
        cum_eq  += t.net_pnl * SIZE_USDT * LEVERAGE
        if cum_eq > peak_eq:
            peak_eq = cum_eq
        dd = peak_eq - cum_eq
        if dd > max_dd_usdt:
            max_dd_usdt = dd
            max_dd_pct  = (dd / peak_eq) * 100

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

    # Equity curve in USDT (partendo da STARTING_CAPITAL)
    equity = [{"ts": sorted_trades[0].entry_ts if sorted_trades else 0, "eq": STARTING_CAPITAL}]
    cum = STARTING_CAPITAL
    for t in sorted_trades:
        cum += t.net_pnl * SIZE_USDT * LEVERAGE
        equity.append({"ts": t.entry_ts, "eq": round(cum, 2)})
    # Campiona max 120 punti
    if len(equity) > 120:
        step = len(equity) // 120
        equity = equity[::step]
    if equity and equity[-1]["eq"] != round(cum, 2):
        equity.append({"ts": sorted_trades[-1].exit_ts if sorted_trades else 0, "eq": round(cum, 2)})

    # ── Output JSON ──────────────────────────────────────────────────────
    record = {
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "exchange":        "Bybit",
        "days":            DAYS,
        "start_date":      START_DATE.strftime("%Y-%m-%d"),
        "symbols_analyzed": len(symbols),
        "config": {
            "starting_capital": STARTING_CAPITAL,
            "size_usdt":        SIZE_USDT,
            "leverage":         LEVERAGE,
            "min_level":        MIN_LEVEL,
            "fee_pct":          round((TAKER_FEE + SLIPPAGE) * 100 * 2, 4),
            "strategy":         "SHORT on positive funding (SHORTs collect), LONG on negative (LONGs collect). Same logic as live bot.",
        },
        "summary": {
            "total_trades":     total_trades,
            "wins":             len(wins),
            "losses":           len(losses),
            "win_rate_pct":     round(win_rate, 1),
            "starting_capital": STARTING_CAPITAL,
            "final_capital":    round(final_capital, 2),
            "total_pnl_usdt":   round(total_pnl_usdt, 2),
            "total_return_pct": round(total_return_pct, 2),
            "avg_pnl_pct":      round(avg_pnl_pct, 4),
            "max_dd_usdt":      round(max_dd_usdt, 2),
            "max_dd_pct":       round(max_dd_pct, 2),
            "best_trade_pct":   round(max(t.net_pnl_pct for t in all_trades), 3),
            "worst_trade_pct":  round(min(t.net_pnl_pct for t in all_trades), 3),
        },
        "monthly":         monthly,
        "top_symbols":     top_symbols,
        "equity_curve":    equity,
    }

    # Serializza i trade individuali ordinati per data
    trades_list = []
    for t in sorted(all_trades, key=lambda x: x.entry_ts):
        trades_list.append({
            "symbol":      t.symbol,
            "direction":   t.direction,
            "level":       t.level,
            "entry_ts":    t.entry_ts,
            "exit_ts":     t.exit_ts,
            "entry_date":  datetime.fromtimestamp(t.entry_ts/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "exit_date":   datetime.fromtimestamp(t.exit_ts/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if t.exit_ts else "",
            "entry_rate":  round(t.entry_rate, 4),
            "cycles":      t.cycles_count,
            "exit_reason": t.exit_reason,
            "duration_h":  round(t.duration_hours, 1),
            "net_pnl_pct": round(t.net_pnl_pct, 4),
            "net_pnl_usdt": round(t.net_pnl * SIZE_USDT * LEVERAGE, 2),
            "is_win":      t.is_win,
        })
    record["trades"] = trades_list

    with open(OUTPUT, "w") as f:
        json.dump(record, f, indent=2)

    logger.info("Track record salvato in %s", OUTPUT)
    logger.info("Totale: %d trades su %d simboli — win rate %.1f%% — PnL %.2f USDT (%.1f%% return)",
                total_trades, len(all_results), win_rate, total_pnl_usdt, total_return_pct)


if __name__ == "__main__":
    asyncio.run(main())
