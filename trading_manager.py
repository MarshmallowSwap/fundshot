"""
trading_manager.py — FundShot SaaS
Gestisce un FundingTrader indipendente per ogni utente/exchange.

Ogni FundingTrader opera con il suo ExchangeClient (API key proprie)
e notifica solo il suo chat_id Telegram.

Uso in bot.py:
    from trading_manager import trading_manager
    await trading_manager.trading_job(context, tickers)
"""

import asyncio
import logging
import math
import time
import json
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional, Callable

from exchanges import ExchangeClient
from user_registry import UserClient
from db.supabase_client import record_trade

logger = logging.getLogger(__name__)

# ── Configurazione default per ogni utente ────────────────────────────────────
DEFAULT_CONFIG = {
    "size_usdt":        50.0,
    "leverage":         2,
    "max_positions":    2,
    "sl_pct":           1.2,
    "tp1_size_pct":     30,
    "trailing_buffer":  {"hard": 1.2, "extreme": 1.0, "high": 0.8, "soft": 0.7},
    "tp1_pct":          {"hard": 1.2, "extreme": 1.0, "high": 0.8, "soft": 0.7},
    "tp_max":           {"hard": 6.0, "extreme": 5.0, "high": 4.0, "soft": 3.0},
    "funding_thresholds": {
        "jackpot": 0.030,
        "hard":    0.020,
        "extreme": 0.015,
        "high":    0.010,
        "soft":    0.005,
    },
    "min_funding_abs":   0.005,
    "min_persistence":   1,
    "mins_before_reset": 30,
    "reopen_cooldown":   1800,  # 30 min
}

FUNDING_RESET_HOURS_UTC = [0, 8, 16]


# ── Dataclass posizione ───────────────────────────────────────────────────────

@dataclass
class TradePosition:
    symbol:            str
    side:              str
    direction:         str
    entry_price:       float
    size_usdt:         float
    notional:          float
    level:             str
    funding_at_open:   float
    oi_change_at_open: float
    tp1_pct:           float
    trailing_buffer:   float
    tp_max_pct:        float
    sl_pct:            float
    sl_price:          float
    tp1_price:         float
    tp1_hit:           bool = False
    tp1_qty:           float = 0.0
    remaining_qty:     float = 0.0
    best_price:        float = 0.0
    trailing_stop:     float = 0.0
    opened_at:         datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    order_id:          str = ""


# ── FundingTrader per singolo utente ─────────────────────────────────────────

class FundingTrader:
    """
    Trader indipendente per un singolo utente.
    Opera su un ExchangeClient con le API key dell'utente.
    """

    def __init__(
        self,
        client: ExchangeClient,
        chat_id: int,
        user_id: str,
        exchange: str,
        send_fn: Callable,          # async fn(chat_id, text)
        config: dict | None = None,
    ):
        self.client   = client
        self.chat_id  = chat_id
        self.user_id  = user_id
        self.exchange = exchange
        self.send     = send_fn
        self.cfg      = {**DEFAULT_CONFIG, **(config or {})}

        self.positions:        dict[str, TradePosition] = {}
        self.persistence:      dict[str, int] = {}
        self._recently_closed: dict[str, float] = {}
        self.results:          list[dict] = []

    # ── LIVELLO FUNDING ───────────────────────────────────────────────────────

    def get_level(self, rate: float) -> Optional[str]:
        abs_r = abs(rate)
        thr   = self.cfg["funding_thresholds"]
        if abs_r >= thr.get("jackpot", 0.03): return "jackpot"
        if abs_r >= thr["hard"]:    return "hard"
        if abs_r >= thr["extreme"]: return "extreme"
        if abs_r >= thr["high"]:    return "high"
        if abs_r >= thr["soft"]:    return "soft"
        return None

    def update_persistence(self, symbol: str, rate: float) -> int:
        if self.get_level(rate):
            self.persistence[symbol] = self.persistence.get(symbol, 0) + 1
        else:
            self.persistence[symbol] = 0
        return self.persistence.get(symbol, 0)

    def mins_to_next_reset(self) -> float:
        now = datetime.now(timezone.utc)
        candidates = []
        for h in FUNDING_RESET_HOURS_UTC:
            t = now.replace(hour=h, minute=0, second=0, microsecond=0)
            if t <= now:
                t += timedelta(days=1)
            candidates.append(t)
        return (min(candidates) - now).total_seconds() / 60

    # ── FILTRI INGRESSO ───────────────────────────────────────────────────────

    async def should_open(self, symbol: str, rate: float) -> tuple[bool, str]:
        if abs(rate) < self.cfg["min_funding_abs"]:
            return False, "funding basso"
        level = self.get_level(rate)
        if not level:
            return False, "nessun livello"
        if self.persistence.get(symbol, 0) < self.cfg["min_persistence"]:
            return False, "persistenza insufficiente"
        if self.mins_to_next_reset() < self.cfg["mins_before_reset"]:
            return False, "troppo vicino al reset"
        if symbol in self.positions:
            return False, "position already open"
        cooldown = self.cfg["reopen_cooldown"]
        if symbol in self._recently_closed:
            elapsed = time.time() - self._recently_closed[symbol]
            if elapsed < cooldown:
                return False, f"cooldown ({int((cooldown-elapsed)/60)} min)"
            del self._recently_closed[symbol]
        if len(self.positions) >= self.cfg["max_positions"]:
            return False, "max posizioni raggiunte"
        return True, "ok"

    # ── APERTURA TRADE ────────────────────────────────────────────────────────

    async def open_trade(self, symbol: str, rate: float):
        level     = self.get_level(rate)
        direction = "SHORT" if rate > 0 else "LONG"
        side      = "Sell" if direction == "SHORT" else "Buy"

        mark = await self.client.get_mark_price(symbol)
        if not mark:
            logger.error("open_trade: no price for %s", symbol)
            return

        oi_data = await self.client.get_open_interest(symbol) or {"change_5m": 0}
        qty     = await self.client.calc_qty(symbol, self.cfg["size_usdt"], self.cfg["leverage"])
        if not qty:
            logger.error("open_trade: no qty for %s", symbol)
            return

        # TP / SL / trailing params
        lvl_key   = level if level in ("hard","extreme","high","soft") else "hard"
        tp1_pct   = self.cfg["tp1_pct"][lvl_key]   / 100
        buf_pct   = self.cfg["trailing_buffer"][lvl_key] / 100
        tp_max    = self.cfg["tp_max"][lvl_key]    / 100
        sl_pct    = self.cfg["sl_pct"]              / 100

        if direction == "SHORT":
            tp1_price = mark * (1 - tp1_pct)
            sl_price  = mark * (1 + sl_pct)
            act_price = mark * (1 - tp1_pct)
        else:
            tp1_price = mark * (1 + tp1_pct)
            sl_price  = mark * (1 - sl_pct)
            act_price = mark * (1 + tp1_pct)

        trail_dist = mark * buf_pct
        use_tp1    = level in ("soft", "high")

        result = await self.client.open_position(
            symbol=symbol, side=side, qty=qty,
            leverage=self.cfg["leverage"],
            sl_pct=self.cfg["sl_pct"],
            tp_pct=tp1_pct * 100 if use_tp1 else 0,
        )
        if not result.ok:
            logger.error("open_trade rejected %s: %s", symbol, result.error)
            return

        await self.client.set_trailing_stop(symbol, trail_dist, act_price)

        notional  = self.cfg["size_usdt"] * self.cfg["leverage"]
        qty_tp1   = round(qty * self.cfg["tp1_size_pct"] / 100, 3)
        qty_rest  = round(qty - qty_tp1, 3)

        pos = TradePosition(
            symbol=symbol, side=side, direction=direction,
            entry_price=mark, size_usdt=self.cfg["size_usdt"],
            notional=notional, level=level, funding_at_open=rate,
            oi_change_at_open=oi_data["change_5m"],
            tp1_pct=tp1_pct*100, trailing_buffer=buf_pct*100,
            tp_max_pct=tp_max*100, sl_pct=self.cfg["sl_pct"],
            sl_price=sl_price, tp1_price=tp1_price,
            tp1_qty=qty_tp1, remaining_qty=qty_rest,
            best_price=mark, trailing_stop=act_price,
            order_id=result.order_id,
        )
        self.positions[symbol] = pos

        emoji = {"jackpot":"🎰","hard":"🔴","extreme":"🔥","high":"🚨","soft":"📊"}.get(level,"📊")
        strat = (
            f"🎯 TP1 30%: `${tp1_price:.6f}` ({tp1_pct*100:+.2f}%) + Trailing {buf_pct*100:.2f}%\n"
            if use_tp1 else
            f"🎯 Trailing 100%: attivo da `${act_price:.6f}`, dist `{buf_pct*100:.2f}%`\n"
        )
        await self.send(self.chat_id,
            f"{emoji} *TRADE APERTO — {direction}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 `{symbol}` | 🏦 {self.exchange.capitalize()}\n"
            f"💰 Entry: `${mark:.6f}` | Funding: `{rate*100:+.4f}%` ({level.upper()})\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{strat}"
            f"🛡️ SL: `${sl_price:.6f}` | ⚡ Leva: `{self.cfg['leverage']}x`\n"
            f"💵 Size: `{self.cfg['size_usdt']} USDT` → `{notional:.0f} USDT` nozionale"
        )
        logger.info("Trade aperto: %s %s @ %.6f [%s]", direction, symbol, mark, level)

    # ── MONITORAGGIO POSIZIONI ────────────────────────────────────────────────

    async def monitor_positions(self):
        for symbol, pos in list(self.positions.items()):
            try:
                await self._monitor_single(symbol, pos)
            except Exception as e:
                logger.error("monitor_positions %s: %s", symbol, e)

    async def _monitor_single(self, symbol: str, pos: TradePosition):
        # Controlla se Bybit ha già chiuso la posizione (TP/SL nativo)
        open_pos = await self.client.get_positions()
        active   = next((p for p in open_pos if p.symbol == symbol), None)
        if active is None or active.size == 0:
            mark = await self.client.get_mark_price(symbol) or pos.entry_price
            is_s = pos.direction == "SHORT"
            pct  = ((pos.entry_price - mark) / pos.entry_price * 100) if is_s \
                   else ((mark - pos.entry_price) / pos.entry_price * 100)
            pnl  = pos.notional * (pct / 100)
            await self.send(self.chat_id,
                f"🔔 *CHIUSA DA EXCHANGE — {pos.direction} {symbol}*\n"
                f"💰 `${mark:.6f}` | {'📈' if pnl>=0 else '📉'} `{pnl:+.2f} USDT` ({pct:+.2f}%)\n"
                f"⏱️ Durata: `{(datetime.now(timezone.utc)-pos.opened_at).seconds//60} min`"
            )
            await self._record(pos, pnl, pct, "EXCHANGE_CLOSE", mark)
            self._recently_closed[symbol] = time.time()
            del self.positions[symbol]
            return

        mark    = await self.client.get_mark_price(symbol)
        if not mark:
            return
        is_short = pos.direction == "SHORT"
        pnl_pct  = ((pos.entry_price - mark) / pos.entry_price * 100) if is_short \
                   else ((mark - pos.entry_price) / pos.entry_price * 100)

        if not pos.tp1_hit:
            tp1_hit = (is_short and mark <= pos.tp1_price) or \
                      (not is_short and mark >= pos.tp1_price)
            if tp1_hit:
                pos.tp1_hit    = True
                pos.best_price = mark
                buf = pos.trailing_buffer / 100
                pos.trailing_stop = mark * (1 + buf) if is_short else mark * (1 - buf)
                pos.sl_price = pos.entry_price
                pnl_tp1 = pos.notional * (pos.tp1_pct/100) * (self.cfg["tp1_size_pct"]/100)
                await self.send(self.chat_id,
                    f"✅ *TP1 COLPITO — {pos.direction} {symbol}*\n"
                    f"💰 `${mark:.4f}` | PnL parz: `+{pnl_tp1:.2f} USDT`\n"
                    f"🔒 SL → breakeven | 🎯 Trailing attivo {pos.trailing_buffer:.1f}%"
                )
                return
            sl_hit = (is_short and mark >= pos.sl_price) or \
                     (not is_short and mark <= pos.sl_price)
            if sl_hit:
                await self._close_full(symbol, pos, "SL", mark, pnl_pct)
        else:
            buf = pos.trailing_buffer / 100
            if is_short:
                if mark < pos.best_price:
                    pos.best_price    = mark
                    pos.trailing_stop = mark * (1 + buf)
            else:
                if mark > pos.best_price:
                    pos.best_price    = mark
                    pos.trailing_stop = mark * (1 - buf)

            if pnl_pct >= pos.tp_max_pct:
                await self._close_remaining(symbol, pos, "TP_MAX", mark, pnl_pct)
                return
            trailing_hit = (is_short and mark >= pos.trailing_stop) or \
                           (not is_short and mark <= pos.trailing_stop)
            if trailing_hit:
                await self._close_remaining(symbol, pos, "TRAILING", mark, pnl_pct)
                return
            sl_hit = (is_short and mark >= pos.sl_price) or \
                     (not is_short and mark <= pos.sl_price)
            if sl_hit:
                await self._close_remaining(symbol, pos, "SL_BREAKEVEN", mark, pnl_pct)

    async def _close_full(self, symbol, pos, reason, price, pct):
        qty = pos.tp1_qty + pos.remaining_qty
        res = await self.client.close_position(symbol, pos.side, qty)
        if res.ok:
            pnl = pos.notional * (pct / 100)
            await self._send_close(pos, reason, price, pnl, pct, "100%")
            await self._record(pos, pnl, pct, reason, price)
            self._recently_closed[symbol] = time.time()
            del self.positions[symbol]

    async def _close_remaining(self, symbol, pos, reason, price, pct):
        res = await self.client.close_position(symbol, pos.side, pos.remaining_qty)
        if res.ok:
            pnl_tp1  = pos.notional * (pos.tp1_pct/100) * (self.cfg["tp1_size_pct"]/100)
            pnl_rest = pos.notional * (pct/100) * (1 - self.cfg["tp1_size_pct"]/100)
            total    = pnl_tp1 + pnl_rest
            await self._send_close(pos, reason, price, total, pct, "70% residuo")
            await self._record(pos, total, pct, reason, price)
            self._recently_closed[symbol] = time.time()
            del self.positions[symbol]

    async def _send_close(self, pos, reason, price, pnl, pct, portion):
        r_map = {
            "TP_MAX": "🎯 TARGET MASSIMO", "TRAILING": "📉 TRAILING STOP",
            "SL": "🛡️ STOP LOSS", "SL_BREAKEVEN": "🔒 BREAKEVEN",
            "FUNDING_EXIT": "🔄 FUNDING RIENTRATO", "EXCHANGE_CLOSE": "🔔 CHIUSA DA EXCHANGE",
        }
        dur = (datetime.now(timezone.utc) - pos.opened_at).seconds // 60
        await self.send(self.chat_id,
            f"{'💚' if pnl>=0 else '🔴'} *TRADE CHIUSO — {r_map.get(reason, reason)}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 `{pos.symbol}` ({pos.direction}) | 🏦 {self.exchange.capitalize()}\n"
            f"💰 Entry: `${pos.entry_price:.4f}` → Exit: `${price:.4f}`\n"
            f"📊 Chiuso: `{portion}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{'📈' if pnl>=0 else '📉'} PnL: `{pnl:+.2f} USDT` ({pct:+.2f}%)\n"
            f"⏱️ Durata: `{dur} min` | Livello: `{pos.level.upper()}`"
        )

    async def _record(self, pos, pnl, pct, reason, exit_price=0.0):
        dur = (datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 60
        self.results.append({
            "symbol": pos.symbol, "direction": pos.direction,
            "pnl_usdt": round(pnl, 4), "pnl_pct": round(pct, 4),
            "duration_min": round(dur, 1), "close_reason": reason, "level": pos.level,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        # Salva su Supabase
        try:
            await record_trade(
                user_id=self.user_id, exchange=self.exchange,
                symbol=pos.symbol, side=pos.side,
                entry_price=pos.entry_price, exit_price=exit_price or pos.entry_price,
                pnl_usdt=round(pnl, 4), level=pos.level,
                opened_at=pos.opened_at.isoformat(),
                closed_at=datetime.now(timezone.utc).isoformat(),
                close_reason=reason,
            )
        except Exception as e:
            logger.warning("record_trade Supabase: %s", e)

    async def check_funding_exit(self, symbol: str, rate: float):
        if symbol not in self.positions:
            return
        if abs(rate) < self.cfg["min_funding_abs"]:
            pos  = self.positions[symbol]
            mark = await self.client.get_mark_price(symbol) or pos.entry_price
            is_s = pos.direction == "SHORT"
            pct  = ((pos.entry_price - mark) / pos.entry_price * 100) if is_s \
                   else ((mark - pos.entry_price) / pos.entry_price * 100)
            if pos.tp1_hit:
                await self._close_remaining(symbol, pos, "FUNDING_EXIT", mark, pct)
            else:
                await self._close_full(symbol, pos, "FUNDING_EXIT", mark, pct)

    def get_stats(self) -> dict:
        if not self.results:
            return {"trades": 0, "open": len(self.positions)}
        wins   = [r for r in self.results if r["pnl_usdt"] > 0]
        losses = [r for r in self.results if r["pnl_usdt"] <= 0]
        total  = sum(r["pnl_usdt"] for r in self.results)
        return {
            "trades":   len(self.results),
            "wins":     len(wins),
            "losses":   len(losses),
            "win_rate": round(len(wins)/len(self.results)*100, 1) if self.results else 0,
            "total_pnl": round(total, 2),
            "avg_win":   round(sum(r["pnl_usdt"] for r in wins)/len(wins), 2) if wins else 0,
            "avg_loss":  round(sum(r["pnl_usdt"] for r in losses)/len(losses), 2) if losses else 0,
            "open":      len(self.positions),
        }


# ── TradingManager — un FundingTrader per utente ──────────────────────────────

class TradingManager:
    """
    Pool di FundingTrader, uno per ogni (chat_id, exchange) attivo.
    Espone trading_job() da chiamare nel job queue di bot.py.
    """

    def __init__(self):
        # { (chat_id, exchange) → FundingTrader }
        self._traders: dict[tuple, FundingTrader] = {}

    def get_or_create(
        self,
        user_client: UserClient,
        send_fn: Callable,
    ) -> FundingTrader:
        key = (user_client.chat_id, user_client.exchange)
        if key not in self._traders:
            self._traders[key] = FundingTrader(
                client=user_client.client,
                chat_id=user_client.chat_id,
                user_id=user_client.user_id,
                exchange=user_client.exchange,
                send_fn=send_fn,
            )
            logger.info(
                "FundingTrader creato: chat_id=%s exchange=%s",
                user_client.chat_id, user_client.exchange,
            )
        return self._traders[key]

    def remove(self, chat_id: int, exchange: str):
        self._traders.pop((chat_id, exchange), None)

    def all_traders(self) -> list[FundingTrader]:
        return list(self._traders.values())

    def get_trader(self, chat_id: int, exchange: str = "bybit") -> FundingTrader | None:
        return self._traders.get((chat_id, exchange))

    async def trading_job(
        self,
        registry,          # UserRegistry
        tickers: list,     # lista FundingTicker dal funding_job
        send_fn: Callable,
        auto_trading: bool = True,
    ):
        """
        Chiamato ogni ciclo dal job queue.
        Per ogni utente attivo: monitora posizioni + cerca nuovi segnali.
        """
        if not auto_trading:
            return

        user_clients = registry.all_clients()
        if not user_clients:
            return

        # Mappa symbol → ticker per lookup veloce
        ticker_map = {t.symbol: t for t in tickers}

        for uc in user_clients:
            try:
                trader = self.get_or_create(uc, send_fn)

                # 1. Monitora posizioni aperte
                if trader.positions:
                    await trader.monitor_positions()

                # 2. Cerca nuovi segnali sui ticker attuali
                for ticker in tickers:
                    sym  = ticker.symbol
                    rate = ticker.funding_rate
                    try:
                        trader.update_persistence(sym, rate)
                        await trader.check_funding_exit(sym, rate)
                        ok, reason = await trader.should_open(sym, rate)
                        if ok:
                            await trader.open_trade(sym, rate)
                        elif trader.persistence.get(sym, 0) > 0:
                            logger.debug(
                                "chat=%s %s rifiutato: %s",
                                uc.chat_id, sym, reason,
                            )
                    except Exception as e:
                        logger.error("trading_job %s/%s: %s", uc.chat_id, sym, e)

            except Exception as e:
                logger.error("trading_job user %s: %s", uc.chat_id, e)


# Istanza globale
trading_manager = TradingManager()
