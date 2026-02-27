"""
backtester.py — Funding King Bot
Motore di backtesting: simula profitti/perdite degli alert degli ultimi 30 giorni.

Strategia simulata:
  - Apre SHORT quando classify() >= HIGH e rate > 0  (guadagni funding dai LONG)
  - Apre LONG  quando classify() >= HIGH e rate < 0  (guadagni funding dagli SHORT)
  - Accumula funding ad ogni ciclo successivo
  - Chiude quando il livello scende sotto RIENTRO, o cambia direzione, o fine dati
  - Deduce fee taker (0.055%) + slippage (0.02%) per apertura e chiusura

Formule P&L:
  SHORT: pnl_funding = sum(rate_pct_cicli)          [rate > 0 → profitto]
  LONG:  pnl_funding = sum(-rate_pct_cicli)          [rate < 0 → profitto]
  net_pnl = pnl_funding - ENTRY_COST - EXIT_COST
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ_IT = ZoneInfo("Europe/Rome")
from typing import Optional

import bybit_client as bc
import alert_logic as al

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# COSTANTI
# ══════════════════════════════════════════════════════════════════════════════

TAKER_FEE      = 0.00055   # 0.055% per lato (Bybit linear perpetual taker)
SLIPPAGE       = 0.0002    # 0.02% slippage stimato
ENTRY_COST     = TAKER_FEE + SLIPPAGE
EXIT_COST      = TAKER_FEE + SLIPPAGE
DAYS_BACK      = 30
MAX_CONCURRENT = 5         # max richieste API parallele per /backtest top10


# ══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    symbol:      str
    direction:   str          # "SHORT" | "LONG"
    level:       str          # "high" | "extreme" | "hard"
    entry_ts:    int          # ms
    exit_ts:     int          # ms  (0 se ancora aperto)
    entry_rate:  float        # funding rate %  all'apertura
    cycles:      list = field(default_factory=list)   # funding rate % incassati
    exit_reason: str  = ""    # "rientro" | "flip" | "end_of_data"

    # ── Proprietà calcolate ───────────────────────────────────────────────

    @property
    def cycles_count(self) -> int:
        return len(self.cycles)

    @property
    def funding_pnl(self) -> float:
        """P&L grezzo dalla raccolta funding (in frazione, non %)."""
        if self.direction == "SHORT":
            return sum(r / 100 for r in self.cycles)
        else:
            return sum(-r / 100 for r in self.cycles)

    @property
    def net_pnl(self) -> float:
        """P&L netto (funding - fee apertura - fee chiusura), in frazione."""
        return self.funding_pnl - ENTRY_COST - EXIT_COST

    @property
    def net_pnl_pct(self) -> float:
        """P&L netto in percentuale."""
        return self.net_pnl * 100

    @property
    def is_win(self) -> bool:
        return self.net_pnl > 0

    @property
    def duration_hours(self) -> float:
        if self.exit_ts and self.entry_ts:
            return (self.exit_ts - self.entry_ts) / 3_600_000
        return 0.0

    @property
    def entry_dt(self) -> str:
        return datetime.fromtimestamp(self.entry_ts / 1000, tz=TZ_IT).strftime("%d/%m %H:%M")

    @property
    def exit_dt(self) -> str:
        if not self.exit_ts:
            return "aperto"
        return datetime.fromtimestamp(self.exit_ts / 1000, tz=TZ_IT).strftime("%d/%m %H:%M")


@dataclass
class BacktestResult:
    symbol:       str
    interval_h:   int
    start_ts:     int
    end_ts:       int
    total_cycles: int
    trades:       list = field(default_factory=list)

    # ── Aggregate properties ──────────────────────────────────────────────

    @property
    def win_trades(self):
        return [t for t in self.trades if t.is_win]

    @property
    def loss_trades(self):
        return [t for t in self.trades if not t.is_win]

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return len(self.win_trades) / len(self.trades) * 100

    @property
    def total_pnl(self) -> float:
        """Somma net_pnl (frazione) di tutti i trade."""
        return sum(t.net_pnl for t in self.trades)

    @property
    def total_pnl_pct(self) -> float:
        return self.total_pnl * 100

    @property
    def avg_pnl(self) -> float:
        if not self.trades:
            return 0.0
        return self.total_pnl / len(self.trades)

    @property
    def avg_pnl_pct(self) -> float:
        return self.avg_pnl * 100

    @property
    def best_trade(self) -> Optional["Trade"]:
        return max(self.trades, key=lambda t: t.net_pnl) if self.trades else None

    @property
    def worst_trade(self) -> Optional["Trade"]:
        return min(self.trades, key=lambda t: t.net_pnl) if self.trades else None

    @property
    def max_drawdown(self) -> float:
        """Max drawdown % cumulativo tra trade sequenziali."""
        if not self.trades:
            return 0.0
        peak = 0.0
        dd   = 0.0
        cum  = 0.0
        for t in self.trades:
            cum += t.net_pnl
            peak = max(peak, cum)
            dd   = max(dd, peak - cum)
        return dd * 100   # in %

    @property
    def avg_cycles_per_trade(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.cycles_count for t in self.trades) / len(self.trades)

    @property
    def avg_duration_hours(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.duration_hours for t in self.trades) / len(self.trades)

    @property
    def start_dt(self) -> str:
        if not self.start_ts:
            return "—"
        return datetime.fromtimestamp(self.start_ts / 1000, tz=TZ_IT).strftime("%d/%m/%Y")

    @property
    def end_dt(self) -> str:
        if not self.end_ts:
            return "—"
        return datetime.fromtimestamp(self.end_ts / 1000, tz=TZ_IT).strftime("%d/%m/%Y")


# ══════════════════════════════════════════════════════════════════════════════
# FETCH STORICO 30 GIORNI
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_30d(symbol: str) -> list[dict]:
    """
    Recupera tutti i cicli di funding degli ultimi 30 giorni per il simbolo.
    Gestisce automaticamente la paginazione (Bybit restituisce max 200 per call).
    Ordine restituito: cronologico (dal più vecchio al più recente).
    """
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - DAYS_BACK * 24 * 3600 * 1000

    all_entries: list[dict] = []
    cursor = ""
    page   = 0

    while True:
        page += 1
        try:
            kwargs: dict = {
                "category":  "linear",
                "symbol":    symbol,
                "startTime": str(start_ms),
                "endTime":   str(now_ms),
                "limit":     200,
            }
            if cursor:
                kwargs["cursor"] = cursor

            res = await bc._run(
                bc.get_session().get_funding_rate_history,
                **kwargs,
            )

            if res.get("retCode") != 0:
                logger.warning("fetch_30d %s p%d: %s", symbol, page, res.get("retMsg"))
                break

            entries = res["result"].get("list", [])
            all_entries.extend(entries)
            logger.debug("fetch_30d %s p%d: %d entries", symbol, page, len(entries))

            cursor = res["result"].get("nextPageCursor", "")
            if not cursor or not entries:
                break

        except Exception as exc:
            logger.error("fetch_30d %s p%d: %s", symbol, page, exc)
            break

    # Ordine cronologico ascendente
    all_entries.sort(key=lambda x: int(x.get("fundingRateTimestamp", 0)))
    return all_entries


# ══════════════════════════════════════════════════════════════════════════════
# FUNZIONI HELPER
# ══════════════════════════════════════════════════════════════════════════════

def _infer_interval(entries: list[dict]) -> int:
    """Inferisce l'intervallo in ore dalla differenza mediana tra timestamp."""
    if len(entries) < 2:
        return 8
    diffs = []
    for i in range(1, min(20, len(entries))):
        d = int(entries[i]["fundingRateTimestamp"]) - int(entries[i - 1]["fundingRateTimestamp"])
        diffs.append(d)
    # mediana per robustezza
    diffs.sort()
    median_ms = diffs[len(diffs) // 2]
    return max(1, round(median_ms / 3_600_000))


# ══════════════════════════════════════════════════════════════════════════════
# ENGINE DI BACKTESTING
# ══════════════════════════════════════════════════════════════════════════════

def run_backtest(symbol: str, entries: list[dict]) -> BacktestResult:
    """
    Simula la logica del bot su dati storici.

    Regole:
      1. Ogni ciclo viene classificato via al.classify(symbol, rate_pct).
      2. Se classify >= "high" → apri posizione (SHORT se rate>0, LONG se rate<0).
      3. Per ogni ciclo aperto → accumula rate in cycles[].
      4. Chiudi se:
           a) classify == "none" e |rate| <= soglia rientro   → exit "rientro"
           b) direzione cambia con livello ancora attivo       → exit "flip" + riapertura
      5. Fine dati e posizione ancora aperta                   → exit "end_of_data"
      6. P&L = funding incassato - fee apertura - fee chiusura
    """
    if not entries:
        return BacktestResult(
            symbol=symbol, interval_h=8,
            start_ts=0, end_ts=0, total_cycles=0,
        )

    interval_h = _infer_interval(entries)
    start_ts   = int(entries[0]["fundingRateTimestamp"])
    end_ts     = int(entries[-1]["fundingRateTimestamp"])

    result: BacktestResult = BacktestResult(
        symbol=symbol,
        interval_h=interval_h,
        start_ts=start_ts,
        end_ts=end_ts,
        total_cycles=len(entries),
    )

    open_trade: Optional[Trade] = None

    for entry in entries:
        ts       = int(entry["fundingRateTimestamp"])
        rate_pct = float(entry.get("fundingRate", 0)) * 100
        abs_rate = abs(rate_pct)
        level    = al.classify(symbol, rate_pct)

        # ── Posizione aperta: accumula e controlla uscita ─────────────────
        if open_trade is not None:
            open_trade.cycles.append(rate_pct)

            rientro_thr   = al.get_effective_threshold(symbol, "rientro")
            should_close  = (level == "none" and abs_rate <= rientro_thr)
            new_dir       = "SHORT" if rate_pct > 0 else "LONG"
            direction_flip = (level not in ("none",) and open_trade.direction != new_dir)

            if should_close or direction_flip:
                open_trade.exit_ts     = ts
                open_trade.exit_reason = "rientro" if should_close else "flip"
                result.trades.append(open_trade)
                open_trade = None

                # Se flip → apri immediatamente nella nuova direzione
                if direction_flip and level != "none":
                    open_trade = Trade(
                        symbol=symbol,
                        direction=new_dir,
                        level=level,
                        entry_ts=ts,
                        exit_ts=0,
                        entry_rate=rate_pct,
                    )

        # ── Nessuna posizione: controlla se aprire ────────────────────────
        if open_trade is None and level in ("high", "extreme", "hard"):
            direction  = "SHORT" if rate_pct > 0 else "LONG"
            open_trade = Trade(
                symbol=symbol,
                direction=direction,
                level=level,
                entry_ts=ts,
                exit_ts=0,
                entry_rate=rate_pct,
            )

    # ── Fine dati: chiudi trade aperto ────────────────────────────────────
    if open_trade is not None and open_trade.cycles:
        open_trade.exit_ts     = end_ts
        open_trade.exit_reason = "end_of_data"
        result.trades.append(open_trade)

    return result


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-SIMBOLO
# ══════════════════════════════════════════════════════════════════════════════

async def run_multi_backtest(
    symbols: list[str],
    max_concurrent: int = MAX_CONCURRENT,
) -> list[BacktestResult]:
    """
    Esegue il backtesting su più simboli con concorrenza limitata.
    Utile per /backtest top10 o /backtest watchlist.
    """
    results: list[BacktestResult] = []
    sem = asyncio.Semaphore(max_concurrent)

    async def _one(sym: str) -> BacktestResult:
        async with sem:
            entries = await fetch_30d(sym)
            return run_backtest(sym, entries)

    tasks = [asyncio.create_task(_one(s)) for s in symbols]
    for task in asyncio.as_completed(tasks):
        try:
            r = await task
            results.append(r)
        except Exception as exc:
            logger.error("run_multi_backtest task error: %s", exc)

    # Ordine alfabetico per output deterministico
    results.sort(key=lambda r: r.symbol)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# FORMATTAZIONE REPORT TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

def _pct(value: float, decimals: int = 3) -> str:
    """Formatta un valore % con segno."""
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.{decimals}f}%"


def format_backtest_report(r: BacktestResult) -> str:
    """Rapporto completo per un singolo simbolo (/backtest SYMBOL)."""

    header = (
        f"📊 *BACKTEST 30GG — {r.symbol}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 {r.start_dt} → {r.end_dt}  |  Ciclo: {r.interval_h}H\n"
        f"🔢 Cicli analizzati: {r.total_cycles}"
    )

    if not r.trades:
        return (
            f"{header}\n\n"
            f"ℹ️ Nessun trade simulato.\n"
            f"Il funding rate di *{r.symbol}* è rimasto sotto soglia\n"
            f"per tutti i 30 giorni analizzati.\n\n"
            f"_Soglia attiva: {al.get_effective_threshold(r.symbol, 'high'):.2f}% (HIGH)_"
        )

    # ── Statistiche principali ────────────────────────────────────────────
    pnl_emoji = "💰" if r.total_pnl_pct >= 0 else "📉"
    wr_emoji  = "✅" if r.win_rate >= 60 else ("⚠️" if r.win_rate >= 40 else "❌")

    lines = [
        header,
        f"🎯 Trade simulati: {len(r.trades)}  ({len(r.win_trades)}W / {len(r.loss_trades)}L)",
        "",
        f"{pnl_emoji} *P&L Totale: {_pct(r.total_pnl_pct)}*",
        f"{wr_emoji} Win rate: {r.win_rate:.0f}%",
        f"📊 P&L medio/trade: {_pct(r.avg_pnl_pct)}",
        f"⏱️ Durata media: {r.avg_cycles_per_trade:.1f} cicli ({r.avg_duration_hours:.1f}H)",
    ]

    if r.max_drawdown > 0:
        lines.append(f"📉 Max drawdown: -{r.max_drawdown:.3f}%")

    # ── Best / Worst ──────────────────────────────────────────────────────
    if r.best_trade:
        bt = r.best_trade
        lines.append(
            f"⚡ Miglior trade: {_pct(bt.net_pnl_pct)}  "
            f"{bt.level.upper()} {bt.direction}  "
            f"{bt.entry_dt} ({bt.cycles_count} cicli)"
        )

    if r.worst_trade and len(r.trades) > 1:
        wt = r.worst_trade
        lines.append(
            f"💀 Peggior trade: {_pct(wt.net_pnl_pct)}  "
            f"{wt.level.upper()} {wt.direction}  "
            f"{wt.entry_dt} ({wt.cycles_count} cicli)"
        )

    # ── TOP 5 trade ───────────────────────────────────────────────────────
    if len(r.trades) >= 3:
        top_n  = min(5, len(r.trades))
        top5   = sorted(r.trades, key=lambda t: t.net_pnl, reverse=True)[:top_n]
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        lines += ["", f"🏆 *TOP {top_n} TRADE:*"]
        for i, t in enumerate(top5):
            dir_arrow = "↓" if t.direction == "SHORT" else "↑"
            lines.append(
                f"{medals[i]} {_pct(t.net_pnl_pct):>9}  "
                f"{dir_arrow}{t.direction} {t.level.upper():<7}  "
                f"{t.entry_dt}  {t.cycles_count}c"
            )

    # ── Breakdown SHORT vs LONG ───────────────────────────────────────────
    short_trades = [t for t in r.trades if t.direction == "SHORT"]
    long_trades  = [t for t in r.trades if t.direction == "LONG"]

    if short_trades and long_trades:
        short_pnl = sum(t.net_pnl_pct for t in short_trades)
        long_pnl  = sum(t.net_pnl_pct for t in long_trades)
        lines += [
            "",
            "📈 *Breakdown direzione:*",
            f"↓ SHORT  {len(short_trades)} trade  P&L {_pct(short_pnl)}",
            f"↑ LONG   {len(long_trades)} trade  P&L {_pct(long_pnl)}",
        ]

    # ── Disclaimer ────────────────────────────────────────────────────────
    lines += [
        "",
        "⚠️ _Simulazione su funding rate storico._",
        "_Non considera spread, liquidazioni forzate o margine._",
        f"_Fee incluse: {TAKER_FEE*100:.3f}% taker + {SLIPPAGE*100:.2f}% slippage/lato_",
    ]

    return "\n".join(lines)


def format_multi_backtest_report(
    results: list[BacktestResult],
    title: str = "TOP SIMBOLI",
) -> str:
    """Rapporto aggregato multi-simbolo per /backtest top10 o /backtest watchlist."""

    results_with_trades = [r for r in results if r.trades]

    header = (
        f"📊 *BACKTEST 30GG — {title}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔍 Simboli analizzati: {len(results)}"
    )

    if not results_with_trades:
        return (
            f"{header}\n\n"
            f"ℹ️ Nessun trade simulato su nessun simbolo.\n"
            f"Tutti i funding rate sono rimasti sotto soglia."
        )

    # Ordine per P&L totale decrescente
    sorted_r    = sorted(results_with_trades, key=lambda r: r.total_pnl, reverse=True)
    total_pnl   = sum(r.total_pnl for r in results_with_trades)
    total_trades = sum(len(r.trades) for r in results_with_trades)
    total_wins  = sum(len(r.win_trades) for r in results_with_trades)
    global_wr   = total_wins / total_trades * 100 if total_trades else 0

    lines = [
        header,
        f"🎯 Trade totali: {total_trades}",
        "",
    ]

    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(sorted_r[:10]):
        medal    = medals[i] if i < 3 else f"{i+1:2d}."
        pnl_str  = _pct(r.total_pnl_pct)
        wr_str   = f"{r.win_rate:.0f}%wr"
        tr_str   = f"{len(r.trades)}t"
        lines.append(f"{medal} `{r.symbol:<12}` {pnl_str:>9}  {wr_str}  {tr_str}")

    # Simboli senza trade
    no_trade = [r.symbol for r in results if not r.trades]
    if no_trade:
        lines += ["", f"_Senza trade: {', '.join(no_trade[:8])}{'...' if len(no_trade)>8 else ''}_"]

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"💰 *P&L aggregato: {_pct(total_pnl * 100)}*",
        f"✅ Win rate globale: {global_wr:.0f}%  ({total_wins}/{total_trades})",
        "",
        "⚠️ _Simulazione su funding rate storico._",
        "_Non considera spread, liquidazioni o margine._",
    ]

    return "\n".join(lines)
