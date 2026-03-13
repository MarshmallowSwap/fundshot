"""
trader.py — Modulo trading automatico per Funding King Bot
Strategia: Mean Reversion su funding estremo
TP: Dinamico a scaglioni ottimizzato per massimizzare il guadagno per trade

Integrazione nel bot esistente:
  - Importa questo modulo in bot.py
  - Chiama trader.run_loop(bot_instance) nel job queue
"""

import asyncio
import logging
import time
import hmac
import hashlib
import json
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIGURAZIONE STRATEGIA
# ─────────────────────────────────────────────

CONFIG = {
    # Risk management
    "size_usdt":        50.0,       # USDT per trade
    "leverage":         2,          # leva fissa
    "max_positions":    2,          # posizioni aperte massime
    "sl_pct":           1.2,        # stop loss fisso %

    # TP scaglioni
    "tp1_size_pct":     30,         # % posizione chiusa a TP1 (30% → lascia correre 70%)
    "trailing_buffer":  {           # trailing stop buffer per livello
        "hard":    1.2,
        "extreme": 1.0,
        "high":    0.8,
        "base":    0.7,
    },
    "tp1_pct":          {           # primo scaglione %
        "hard":    1.2,
        "extreme": 1.0,
        "high":    0.8,
        "base":    0.7,
    },
    "tp_max":           {           # cap massimo %
        "hard":    6.0,
        "extreme": 5.0,
        "high":    4.0,
        "base":    3.0,
    },

    # Filtri di ingresso
    "funding_thresholds": {
        "hard":    0.020,
        "extreme": 0.015,
        "high":    0.010,
        "base":    0.005,
    },
    "min_funding_abs":      0.005,  # funding minimo assoluto per entrare
    "min_oi_change_5m":     1.5,    # OI deve crescere almeno 1.5% in 5min
    "min_persistence":      2,      # periodi consecutivi sopra soglia
    "mins_before_reset":    30,     # minuti minimi al prossimo reset funding

    # Timing
    "loop_interval_sec":    60,     # controllo ogni 60 secondi
    "monitor_interval_sec": 15,     # monitora posizioni aperte ogni 15s

    # Bybit
    "category":    "linear",
    "coin":        "USDT",
    "recv_window": "5000",
}


def load_config(path: str = "trader_config.json") -> None:
    """
    Carica la configurazione da un file JSON (esportato dalla dashboard).
    Sovrascrive i valori in CONFIG mantenendo le chiavi interne intatte.

    Struttura attesa (compatibile con l'export della dashboard):
    {
        "size":     50,
        "leva":     2,
        "maxpos":   2,
        "sl":       1.2,
        "tp1pct":   30,
        "persist":  2,
        "oi":       1.5,
        "minreset": 30,
        "cooldown": 0,
        "tp": {
            "jackpot": [1.2, 1.2, 6.0],
            "hard":    [1.2, 1.2, 6.0],
            "extreme": [1.0, 1.0, 5.0],
            "high":    [0.8, 0.8, 4.0]
        },
        "thr": {
            "jackpot": 2.50,
            "hard":    2.00,
            "extreme": 1.50,
            "high":    1.00,
            "close":   0.75
        }
    }

    Chiamata in bot.py prima di creare FundingTrader:
        from trader import CONFIG, load_config
        load_config("trader_config.json")
    """
    import os
    if not os.path.exists(path):
        logger.info(f"load_config: {path} non trovato, uso defaults")
        return

    try:
        with open(path, "r") as f:
            c = json.load(f)

        CONFIG["size_usdt"]     = float(c.get("size",     CONFIG["size_usdt"]))
        CONFIG["leverage"]      = int(c.get("leva",       CONFIG["leverage"]))
        CONFIG["max_positions"] = int(c.get("maxpos",     CONFIG["max_positions"]))
        CONFIG["sl_pct"]        = float(c.get("sl",       CONFIG["sl_pct"]))
        CONFIG["tp1_size_pct"]  = int(c.get("tp1pct",     CONFIG["tp1_size_pct"]))
        CONFIG["min_persistence"]   = int(c.get("persist",   CONFIG["min_persistence"]))
        CONFIG["min_oi_change_5m"]  = float(c.get("oi",      CONFIG["min_oi_change_5m"]))
        CONFIG["mins_before_reset"] = int(c.get("minreset",  CONFIG["mins_before_reset"]))

        # TP per livello dal formato dashboard: {"hard": [tp1, trail, cap], ...}
        tp_map = {
            "jackpot": "hard",   # jackpot usa gli stessi parametri di hard
            "hard":    "hard",
            "extreme": "extreme",
            "high":    "high",
        }
        tp = c.get("tp", {})
        for dash_key, cfg_key in tp_map.items():
            if dash_key in tp and len(tp[dash_key]) >= 3:
                v = tp[dash_key]
                CONFIG["tp1_pct"][cfg_key]        = float(v[0])
                CONFIG["trailing_buffer"][cfg_key] = float(v[1])
                CONFIG["tp_max"][cfg_key]          = float(v[2])

        # Soglie alert → funding_thresholds
        thr = c.get("thr", {})
        if "hard"    in thr: CONFIG["funding_thresholds"]["hard"]    = float(thr["hard"])    / 100
        if "extreme" in thr: CONFIG["funding_thresholds"]["extreme"] = float(thr["extreme"]) / 100
        if "high"    in thr: CONFIG["funding_thresholds"]["high"]    = float(thr["high"])    / 100

        logger.info(
            f"Configurazione caricata da {path}: "
            f"size={CONFIG['size_usdt']} USDT, leva={CONFIG['leverage']}x, "
            f"maxpos={CONFIG['max_positions']}, SL={CONFIG['sl_pct']}%"
        )
    except Exception as e:
        logger.error(f"load_config error: {e} — uso defaults")

# Orari reset funding Bybit (UTC): 00:00, 08:00, 16:00
FUNDING_RESET_HOURS_UTC = [0, 8, 16]


# ─────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────

@dataclass
class TradePosition:
    symbol:          str
    side:            str            # "Buy" o "Sell"
    direction:       str            # "LONG" o "SHORT"
    entry_price:     float
    size_usdt:       float
    notional:        float
    level:           str            # hard / extreme / high / base
    funding_at_open: float
    oi_change_at_open: float
    tp1_pct:         float
    trailing_buffer: float
    tp_max_pct:      float
    sl_pct:          float
    sl_price:        float
    tp1_price:       float
    tp1_hit:         bool = False
    tp1_qty:         float = 0.0    # qty chiusa a TP1
    remaining_qty:   float = 0.0    # qty che segue il trailing
    best_price:      float = 0.0    # miglior prezzo raggiunto (per trailing)
    trailing_stop:   float = 0.0    # livello trailing corrente
    opened_at:       datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    bybit_order_id:  str = ""


@dataclass
class TradeResult:
    symbol:       str
    direction:    str
    pnl_usdt:     float
    pnl_pct:      float
    duration_min: float
    close_reason: str            # TP1, TRAILING, SL, FUNDING_EXIT, MANUAL
    level:        str


# ─────────────────────────────────────────────
# BYBIT CLIENT PRIVATO
# ─────────────────────────────────────────────

class BybitTrader:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True, demo: bool = False):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.base_url   = (
            "https://api-demo.bybit.com" if demo else ("https://api-testnet.bybit.com" if testnet
            else "https://api.bybit.com")
        )
        self.testnet = testnet
        self.demo = demo
        logger.info(f"BybitTrader init — {'DEMO' if demo else ('TESTNET' if testnet else 'MAINNET')}")

    def _sign(self, params: str) -> tuple[str, str]:
        ts = str(int(time.time() * 1000))
        pre_sign = ts + self.api_key + CONFIG["recv_window"] + params
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            pre_sign.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return ts, signature

    def _headers(self, ts: str, sign: str) -> dict:
        return {
            "X-BAPI-API-KEY":      self.api_key,
            "X-BAPI-TIMESTAMP":    ts,
            "X-BAPI-SIGN":         sign,
            "X-BAPI-RECV-WINDOW":  CONFIG["recv_window"],
            "Content-Type":        "application/json",
        }

    def _get(self, path: str, params: dict) -> dict:
        qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        ts, sign = self._sign(qs)
        r = requests.get(
            f"{self.base_url}{path}?{qs}",
            headers=self._headers(ts, sign),
            timeout=10
        )
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        body_str = json.dumps(body)
        ts, sign = self._sign(body_str)
        r = requests.post(
            f"{self.base_url}{path}",
            headers=self._headers(ts, sign),
            data=body_str,
            timeout=10
        )
        return r.json()

    # ── MARKET DATA ──

    def get_ticker(self, symbol: str) -> Optional[dict]:
        try:
            r = requests.get(
                f"https://api.bybit.com/v5/market/tickers",
                params={"category": "linear", "symbol": symbol},
                timeout=10
            )
            data = r.json()
            if data["retCode"] == 0 and data["result"]["list"]:
                return data["result"]["list"][0]
        except Exception as e:
            logger.error(f"get_ticker {symbol}: {e}")
        return None

    def get_open_interest(self, symbol: str) -> Optional[dict]:
        try:
            r = requests.get(
                "https://api.bybit.com/v5/market/open-interest",
                params={"category": "linear", "symbol": symbol,
                        "intervalTime": "5min", "limit": 3},
                timeout=10
            )
            data = r.json()
            if data["retCode"] != 0:
                return None
            items = data["result"]["list"]
            curr  = float(items[0]["openInterest"])
            prev  = float(items[1]["openInterest"])
            prev2 = float(items[2]["openInterest"])
            return {
                "oi":          curr,
                "change_5m":   (curr - prev)  / prev  * 100 if prev  else 0,
                "change_10m":  (curr - prev2) / prev2 * 100 if prev2 else 0,
            }
        except Exception as e:
            logger.error(f"get_oi {symbol}: {e}")
        return None

    def get_mark_price(self, symbol: str) -> Optional[float]:
        ticker = self.get_ticker(symbol)
        if ticker:
            return float(ticker.get("markPrice", 0))
        return None

    # ── TRADING ──

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        r = self._post("/v5/position/set-leverage", {
            "category":   CONFIG["category"],
            "symbol":     symbol,
            "buyLeverage":  str(leverage),
            "sellLeverage": str(leverage),
        })
        return r.get("retCode") == 0

    def get_lot_info(self, symbol: str) -> dict:
        """Ottieni minOrderQty e qtyStep dal lotSizeFilter di Bybit."""
        try:
            r = requests.get(
                "https://api.bybit.com/v5/market/instruments-info",
                params={"category": "linear", "symbol": symbol},
                timeout=10
            )
            data = r.json()
            if data["retCode"] == 0:
                info = data["result"]["list"][0]["lotSizeFilter"]
                return {
                    "min_qty":  float(info.get("minOrderQty", 0.001)),
                    "qty_step": float(info.get("qtyStep", 0.001)),
                }
        except Exception as e:
            logger.error(f"get_lot_info {symbol}: {e}")
        return {"min_qty": 0.001, "qty_step": 0.001}

    def get_min_qty(self, symbol: str) -> float:
        return self.get_lot_info(symbol)["min_qty"]

    def calc_qty(self, symbol: str, size_usdt: float, leverage: int) -> Optional[float]:
        """Calcola qty rispettando minOrderQty e qtyStep di Bybit."""
        price = self.get_mark_price(symbol)
        if not price:
            return None
        lot   = self.get_lot_info(symbol)
        min_q = lot["min_qty"]
        step  = lot["qty_step"]
        notional = size_usdt * leverage
        raw_qty  = notional / price
        # Arrotonda al multiplo di step
        import math
        decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
        qty = round(math.floor(raw_qty / step) * step, decimals)
        qty = max(qty, min_q)
        logger.debug(f"calc_qty {symbol}: price={price} notional={notional} raw={raw_qty:.4f} step={step} qty={qty}")
        return qty

    def place_order(self, symbol: str, side: str, qty: float,
                    sl_price: float, tp_price: float) -> Optional[str]:
        """
        Apre un ordine market specificando la size in USDT (marketUnit: quoteCoin).
        Bybit calcola automaticamente la qty in base al prezzo corrente.
        side: "Buy" (long) o "Sell" (short)
        """
        self.set_leverage(symbol, CONFIG["leverage"])

        # Nozionale in USDT = size * leverage
        notional_usdt = CONFIG["size_usdt"] * CONFIG["leverage"]

        body = {
            "category":       CONFIG["category"],
            "symbol":         symbol,
            "side":           side,
            "orderType":      "Market",
            "qty":            str(round(notional_usdt, 2)),
            "marketUnit":     "quoteCoin",   # specifica in USDT, Bybit calcola la qty
            "stopLoss":       str(round(sl_price, 6)),
            "takeProfit":     str(round(tp_price, 6)),
            "slTriggerBy":    "MarkPrice",
            "tpTriggerBy":    "MarkPrice",
            "timeInForce":    "IOC",
            "reduceOnly":     False,
            "closeOnTrigger": False,
        }
        r = self._post("/v5/order/create", body)
        if r.get("retCode") == 0:
            return r["result"]["orderId"]
        logger.error(f"place_order error: {r.get('retMsg')} | {symbol} {side} {notional_usdt}USDT")
        return None

    def close_position(self, symbol: str, side: str, qty: float) -> bool:
        """Chiude (parzialmente o totalmente) una posizione al mercato."""
        close_side = "Buy" if side == "Sell" else "Sell"
        body = {
            "category":      CONFIG["category"],
            "symbol":        symbol,
            "side":          close_side,
            "orderType":     "Market",
            "qty":           str(qty),
            "timeInForce":   "IOC",
            "reduceOnly":    True,
        }
        r = self._post("/v5/order/create", body)
        ok = r.get("retCode") == 0
        if not ok:
            logger.error(f"close_position error: {r.get('retMsg')}")
        return ok

    def get_position(self, symbol: str) -> Optional[dict]:
        r = self._get("/v5/position/list", {
            "category": CONFIG["category"],
            "symbol":   symbol,
        })
        if r.get("retCode") == 0:
            positions = [p for p in r["result"]["list"] if float(p.get("size", 0)) > 0]
            return positions[0] if positions else None
        return None

    def get_wallet_balance(self) -> Optional[dict]:
        r = self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        if r.get("retCode") == 0:
            return r["result"]["list"][0]
        return None


# ─────────────────────────────────────────────
# LOGICA STRATEGIA
# ─────────────────────────────────────────────

class FundingTrader:
    def __init__(self, bybit: BybitTrader, telegram_send_fn):
        self.bybit         = bybit
        self.send          = telegram_send_fn      # async fn(chat_id, msg)
        self.positions:    dict[str, TradePosition] = {}
        self.persistence:  dict[str, int]           = {}
        self.results:      list[TradeResult]        = []
        self.chat_id:      Optional[str]            = None

    # ── FILTRI INGRESSO ──

    def get_level(self, funding_rate: float) -> Optional[str]:
        abs_rate = abs(funding_rate)
        thr = CONFIG["funding_thresholds"]
        if abs_rate >= thr["hard"]:    return "hard"
        if abs_rate >= thr["extreme"]: return "extreme"
        if abs_rate >= thr["high"]:    return "high"
        if abs_rate >= thr["base"]:    return "base"
        return None

    def update_persistence(self, symbol: str, funding_rate: float) -> int:
        level = self.get_level(funding_rate)
        if level:
            self.persistence[symbol] = self.persistence.get(symbol, 0) + 1
        else:
            self.persistence[symbol] = 0
        return self.persistence.get(symbol, 0)

    def mins_to_next_reset(self) -> float:
        now = datetime.now(timezone.utc)
        next_resets = []
        for h in FUNDING_RESET_HOURS_UTC:
            candidate = now.replace(hour=h, minute=0, second=0, microsecond=0)
            if candidate <= now:
                # passa al giorno dopo se già passato
                from datetime import timedelta
                candidate += timedelta(days=1)
            next_resets.append(candidate)
        next_reset = min(next_resets)
        return (next_reset - now).total_seconds() / 60

    def calc_trade_params(self, entry_price: float, direction: str, level: str) -> dict:
        """Calcola tutti i prezzi TP1, SL, trailing in base al livello."""
        tp1_pct       = CONFIG["tp1_pct"][level]       / 100
        trailing_buf  = CONFIG["trailing_buffer"][level] / 100
        tp_max_pct    = CONFIG["tp_max"][level]         / 100
        sl_pct        = CONFIG["sl_pct"]                / 100

        if direction == "SHORT":
            tp1_price    = entry_price * (1 - tp1_pct)
            sl_price     = entry_price * (1 + sl_pct)
            tp_max_price = entry_price * (1 - tp_max_pct)
        else:  # LONG
            tp1_price    = entry_price * (1 + tp1_pct)
            sl_price     = entry_price * (1 - sl_pct)
            tp_max_price = entry_price * (1 + tp_max_pct)

        return {
            "tp1_price":      tp1_price,
            "sl_price":       sl_price,
            "tp_max_price":   tp_max_price,
            "tp1_pct":        tp1_pct * 100,
            "trailing_buffer": trailing_buf * 100,
            "tp_max_pct":     tp_max_pct * 100,
        }

    async def should_open(self, symbol: str, funding_rate: float) -> tuple[bool, str]:
        """Verifica tutti i filtri. Ritorna (ok, motivo_rifiuto)."""

        # 1. Funding sopra soglia minima
        if abs(funding_rate) < CONFIG["min_funding_abs"]:
            return False, "funding troppo basso"

        # 2. Livello riconoscibile
        level = self.get_level(funding_rate)
        if not level:
            return False, "nessun livello"

        # 3. Persistenza minima
        periods = self.persistence.get(symbol, 0)
        if periods < CONFIG["min_persistence"]:
            return False, f"persistenza insufficiente ({periods}/{CONFIG['min_persistence']})"

        # 4. Non troppo vicino al reset funding
        mins_left = self.mins_to_next_reset()
        if mins_left < CONFIG["mins_before_reset"]:
            return False, f"troppo vicino al reset ({mins_left:.0f} min)"

        # 5. Posizione già aperta su questo simbolo
        if symbol in self.positions:
            return False, "posizione già aperta"

        # 6. Max posizioni raggiunte
        if len(self.positions) >= CONFIG["max_positions"]:
            return False, f"max posizioni raggiunte ({CONFIG['max_positions']})"

        # 7. OI in crescita
        oi_data = self.bybit.get_open_interest(symbol)
        if not oi_data:
            return False, "OI non disponibile"
        if oi_data["change_5m"] < CONFIG["min_oi_change_5m"]:
            return False, f"OI insufficiente ({oi_data['change_5m']:.2f}%)"

        return True, "ok"

    # ── APERTURA ──

    async def open_trade(self, symbol: str, funding_rate: float, chat_id: str):
        level     = self.get_level(funding_rate)
        direction = "SHORT" if funding_rate > 0 else "LONG"
        side      = "Sell"  if direction == "SHORT" else "Buy"

        mark_price = self.bybit.get_mark_price(symbol)
        if not mark_price:
            logger.error(f"open_trade: impossibile ottenere prezzo {symbol}")
            return

        oi_data  = self.bybit.get_open_interest(symbol) or {"change_5m": 0}
        params   = self.calc_trade_params(mark_price, direction, level)
        notional = CONFIG["size_usdt"] * CONFIG["leverage"]

        qty = self.bybit.calc_qty(symbol, CONFIG["size_usdt"], CONFIG["leverage"])
        if not qty:
            logger.error(f"open_trade: impossibile calcolare qty {symbol}")
            return

        # qty per TP1 (30%) e residuo (70%)
        qty_tp1       = round(qty * CONFIG["tp1_size_pct"] / 100, 3)
        qty_remaining = round(qty - qty_tp1, 3)

        order_id = self.bybit.place_order(
            symbol    = symbol,
            side      = side,
            qty       = qty,
            sl_price  = params["sl_price"],
            tp_price  = params["tp1_price"],   # TP bybit = primo scaglione
        )

        if not order_id:
            logger.error(f"open_trade: ordine rifiutato {symbol}")
            return

        # Calcola trailing stop iniziale
        if direction == "SHORT":
            trailing_stop = mark_price * (1 - params["trailing_buffer"] / 100)
        else:
            trailing_stop = mark_price * (1 + params["trailing_buffer"] / 100)

        pos = TradePosition(
            symbol           = symbol,
            side             = side,
            direction        = direction,
            entry_price      = mark_price,
            size_usdt        = CONFIG["size_usdt"],
            notional         = notional,
            level            = level,
            funding_at_open  = funding_rate,
            oi_change_at_open= oi_data["change_5m"],
            tp1_pct          = params["tp1_pct"],
            trailing_buffer  = params["trailing_buffer"],
            tp_max_pct       = params["tp_max_pct"],
            sl_pct           = CONFIG["sl_pct"],
            sl_price         = params["sl_price"],
            tp1_price        = params["tp1_price"],
            tp1_hit          = False,
            tp1_qty          = qty_tp1,
            remaining_qty    = qty_remaining,
            best_price       = mark_price,
            trailing_stop    = trailing_stop,
            bybit_order_id   = order_id,
        )

        self.positions[symbol] = pos

        level_emoji = {"hard":"🔴","extreme":"🔥","high":"🚨","base":"ℹ️"}[level]
        msg = (
            f"{level_emoji} *TRADE APERTO — {direction}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 Coppia:    `{symbol}`\n"
            f"💰 Entry:     `${mark_price:.4f}`\n"
            f"📊 Funding:   `{funding_rate*100:+.4f}%` ({level.upper()})\n"
            f"📈 OI Δ5m:    `{oi_data['change_5m']:+.2f}%`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎯 TP1 (30%): `${params['tp1_price']:.4f}` ({params['tp1_pct']:+.2f}%)\n"
            f"🎯 TP max:    `~{params['tp_max_pct']:.1f}%` (trailing)\n"
            f"🛡️ SL:        `${params['sl_price']:.4f}` (-{CONFIG['sl_pct']:.1f}%)\n"
            f"⚡ Leva:      `{CONFIG['leverage']}x`\n"
            f"💵 Size:      `{CONFIG['size_usdt']} USDT` (notionale: `{notional:.0f} USDT`)\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🆔 Order ID: `{order_id}`"
        )
        await self.send(chat_id, msg)
        logger.info(f"Trade aperto: {direction} {symbol} @ {mark_price} | level={level}")

    # ── MONITORAGGIO ──

    async def monitor_positions(self, chat_id: str):
        """Gestisce trailing stop e chiusure per tutte le posizioni aperte."""
        for symbol, pos in list(self.positions.items()):
            try:
                await self._monitor_single(symbol, pos, chat_id)
            except Exception as e:
                logger.error(f"monitor_positions {symbol}: {e}")

    async def _monitor_single(self, symbol: str, pos: TradePosition, chat_id: str):
        mark_price = self.bybit.get_mark_price(symbol)
        if not mark_price:
            return

        is_short = pos.direction == "SHORT"
        pnl_pct  = ((pos.entry_price - mark_price) / pos.entry_price * 100) if is_short \
                   else ((mark_price - pos.entry_price) / pos.entry_price * 100)

        # ── FASE 1: Prima che TP1 sia colpito ──
        if not pos.tp1_hit:
            # Controlla se TP1 è stato raggiunto
            tp1_hit = (is_short and mark_price <= pos.tp1_price) or \
                      (not is_short and mark_price >= pos.tp1_price)

            if tp1_hit:
                pos.tp1_hit   = True
                pos.best_price = mark_price

                # Bybit ha già chiuso il 30% con il TP impostato
                # Aggiorniamo il trailing stop per il residuo 70%
                buf = pos.trailing_buffer / 100
                if is_short:
                    pos.trailing_stop = mark_price * (1 + buf)
                else:
                    pos.trailing_stop = mark_price * (1 - buf)

                pnl_tp1 = CONFIG["size_usdt"] * CONFIG["leverage"] * (pos.tp1_pct / 100) * (CONFIG["tp1_size_pct"] / 100)

                msg = (
                    f"✅ *TP1 COLPITO — {pos.direction} {symbol}*\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"💰 Prezzo:    `${mark_price:.4f}`\n"
                    f"💵 Chiuso:    `30%` della posizione\n"
                    f"📈 PnL parz:  `+{pnl_tp1:.2f} USDT`\n"
                    f"🔄 SL spostato a breakeven: `${pos.entry_price:.4f}`\n"
                    f"🎯 Trailing attivo: buffer `{pos.trailing_buffer:.1f}%`\n"
                    f"⏳ 70% posizione ancora aperta..."
                )
                await self.send(chat_id, msg)
                # Sposta SL a breakeven su Bybit
                pos.sl_price = pos.entry_price
                logger.info(f"TP1 colpito {symbol} @ {mark_price}")
                return

            # Controlla SL prima di TP1
            sl_hit = (is_short and mark_price >= pos.sl_price) or \
                     (not is_short and mark_price <= pos.sl_price)
            if sl_hit:
                await self._close_full(symbol, pos, chat_id, "SL", mark_price, pnl_pct)
                return

        # ── FASE 2: Dopo TP1, gestione trailing sul 70% ──
        else:
            buf = pos.trailing_buffer / 100

            # Aggiorna best price e trailing stop
            if is_short:
                if mark_price < pos.best_price:
                    pos.best_price    = mark_price
                    pos.trailing_stop = mark_price * (1 + buf)
            else:
                if mark_price > pos.best_price:
                    pos.best_price    = mark_price
                    pos.trailing_stop = mark_price * (1 - buf)

            # Controlla cap massimo
            max_hit = (is_short and pnl_pct >= pos.tp_max_pct) or \
                      (not is_short and pnl_pct >= pos.tp_max_pct)
            if max_hit:
                await self._close_remaining(symbol, pos, chat_id, "TP_MAX", mark_price, pnl_pct)
                return

            # Controlla trailing stop
            trailing_hit = (is_short and mark_price >= pos.trailing_stop) or \
                           (not is_short and mark_price <= pos.trailing_stop)
            if trailing_hit:
                await self._close_remaining(symbol, pos, chat_id, "TRAILING", mark_price, pnl_pct)
                return

            # Controlla SL (breakeven dopo TP1)
            sl_hit = (is_short and mark_price >= pos.sl_price) or \
                     (not is_short and mark_price <= pos.sl_price)
            if sl_hit:
                await self._close_remaining(symbol, pos, chat_id, "SL_BREAKEVEN", mark_price, pnl_pct)
                return

    async def _close_full(self, symbol: str, pos: TradePosition, chat_id: str,
                          reason: str, price: float, pnl_pct: float):
        """Chiude l'intera posizione."""
        full_qty = pos.tp1_qty + pos.remaining_qty
        ok = self.bybit.close_position(symbol, pos.side, full_qty)
        if ok:
            pnl = pos.notional * (pnl_pct / 100)
            await self._send_close_msg(chat_id, pos, reason, price, pnl, pnl_pct, "100%")
            self._record_result(pos, pnl, pnl_pct, reason)
            del self.positions[symbol]

    async def _close_remaining(self, symbol: str, pos: TradePosition, chat_id: str,
                                reason: str, price: float, pnl_pct: float):
        """Chiude il 70% residuo dopo TP1."""
        ok = self.bybit.close_position(symbol, pos.side, pos.remaining_qty)
        if ok:
            # PnL totale = TP1 parziale + residuo
            pnl_tp1  = pos.notional * (pos.tp1_pct / 100) * (CONFIG["tp1_size_pct"] / 100)
            pnl_rest = pos.notional * (pnl_pct / 100) * (1 - CONFIG["tp1_size_pct"] / 100)
            total_pnl = pnl_tp1 + pnl_rest
            await self._send_close_msg(chat_id, pos, reason, price, total_pnl, pnl_pct, "70% residuo")
            self._record_result(pos, total_pnl, pnl_pct, reason)
            del self.positions[symbol]

    async def _send_close_msg(self, chat_id: str, pos: TradePosition, reason: str,
                               price: float, pnl: float, pnl_pct: float, portion: str):
        emoji  = "💚" if pnl >= 0 else "🔴"
        r_map  = {"TP_MAX":"🎯 TARGET MASSIMO","TRAILING":"📉 TRAILING STOP",
                  "SL":"🛡️ STOP LOSS","SL_BREAKEVEN":"🔒 BREAKEVEN","FUNDING_EXIT":"🔄 FUNDING RIENTRATO"}
        reason_str = r_map.get(reason, reason)
        duration   = (datetime.now(timezone.utc) - pos.opened_at).seconds // 60

        msg = (
            f"{emoji} *TRADE CHIUSO — {reason_str}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 Coppia:    `{pos.symbol}` ({pos.direction})\n"
            f"💰 Entry:     `${pos.entry_price:.4f}`\n"
            f"💰 Exit:      `${price:.4f}`\n"
            f"📊 Chiuso:    `{portion}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{'📈' if pnl >= 0 else '📉'} PnL:       `{pnl:+.2f} USDT` ({pnl_pct:+.2f}%)\n"
            f"⏱️ Durata:    `{duration} min`\n"
            f"📋 Livello:   `{pos.level.upper()}`"
        )
        await self.send(chat_id, msg)
        logger.info(f"Trade chiuso: {pos.direction} {pos.symbol} | {reason} | PnL: {pnl:+.2f} USDT")

    def _record_result(self, pos: TradePosition, pnl: float, pnl_pct: float, reason: str):
        duration = (datetime.now(timezone.utc) - pos.opened_at).seconds / 60
        self.results.append(TradeResult(
            symbol       = pos.symbol,
            direction    = pos.direction,
            pnl_usdt     = pnl,
            pnl_pct      = pnl_pct,
            duration_min = duration,
            close_reason = reason,
            level        = pos.level,
        ))

    # ── FUNDING EXIT ──

    async def check_funding_exit(self, symbol: str, funding_rate: float, chat_id: str):
        """Chiude posizione se il funding è rientrato."""
        if symbol not in self.positions:
            return
        pos = self.positions[symbol]
        abs_rate = abs(funding_rate)

        # Esci se funding è rientrato sotto la soglia base
        if abs_rate < CONFIG["min_funding_abs"]:
            mark_price = self.bybit.get_mark_price(symbol) or pos.entry_price
            is_short   = pos.direction == "SHORT"
            pnl_pct    = ((pos.entry_price - mark_price) / pos.entry_price * 100) if is_short \
                         else ((mark_price - pos.entry_price) / pos.entry_price * 100)

            if pos.tp1_hit:
                await self._close_remaining(symbol, pos, chat_id, "FUNDING_EXIT", mark_price, pnl_pct)
            else:
                await self._close_full(symbol, pos, chat_id, "FUNDING_EXIT", mark_price, pnl_pct)

    # ── STATISTICHE ──

    def get_stats(self) -> dict:
        if not self.results:
            return {"trades": 0}
        wins    = [r for r in self.results if r.pnl_usdt > 0]
        losses  = [r for r in self.results if r.pnl_usdt <= 0]
        total   = sum(r.pnl_usdt for r in self.results)
        win_rate= len(wins) / len(self.results) * 100 if self.results else 0
        return {
            "trades":     len(self.results),
            "wins":       len(wins),
            "losses":     len(losses),
            "win_rate":   round(win_rate, 1),
            "total_pnl":  round(total, 2),
            "avg_win":    round(sum(r.pnl_usdt for r in wins) / len(wins), 2) if wins else 0,
            "avg_loss":   round(sum(r.pnl_usdt for r in losses) / len(losses), 2) if losses else 0,
            "open":       len(self.positions),
        }

    # ── LOOP PRINCIPALE ──

    async def run_loop(self, symbols: list[str], chat_id: str,
                       get_funding_fn, interval: int = 60):
        """
        Loop principale da avviare nel job queue del bot.

        Args:
            symbols:        lista simboli da monitorare (es. ["BTCUSDT","ETHUSDT"])
            chat_id:        ID chat Telegram dove mandare gli alert
            get_funding_fn: funzione che ritorna il funding rate per un simbolo
            interval:       secondi tra un controllo e l'altro
        """
        logger.info(f"FundingTrader loop avviato — {len(symbols)} simboli")
        self.chat_id = chat_id

        while True:
            try:
                # 1. Monitora posizioni aperte (ogni ciclo)
                if self.positions:
                    await self.monitor_positions(chat_id)

                # 2. Cerca nuovi segnali
                for symbol in symbols:
                    try:
                        funding_rate = await get_funding_fn(symbol)
                        if funding_rate is None:
                            continue

                        # Aggiorna persistenza
                        periods = self.update_persistence(symbol, funding_rate)

                        # Controlla exit su posizioni esistenti
                        await self.check_funding_exit(symbol, funding_rate, chat_id)

                        # Cerca nuove aperture
                        ok, reason = await self.should_open(symbol, funding_rate)
                        if ok:
                            await self.open_trade(symbol, funding_rate, chat_id)
                        elif periods > 0:
                            logger.debug(f"{symbol}: segnale rifiutato — {reason}")

                    except Exception as e:
                        logger.error(f"run_loop symbol {symbol}: {e}")

            except Exception as e:
                logger.error(f"run_loop outer: {e}")

            await asyncio.sleep(interval)


# ─────────────────────────────────────────────
# INTEGRAZIONE NEL BOT ESISTENTE
# ─────────────────────────────────────────────
#
# In bot.py aggiungi:
#
#   from trader import BybitTrader, FundingTrader
#
#   bybit_trader = BybitTrader(
#       api_key    = os.getenv("BYBIT_API_KEY"),
#       api_secret = os.getenv("BYBIT_API_SECRET"),
#       testnet    = True   # ← metti False solo quando sei pronto per il live
#   )
#
#   async def telegram_send(chat_id, msg):
#       await application.bot.send_message(
#           chat_id    = chat_id,
#           text       = msg,
#           parse_mode = "Markdown"
#       )
#
#   funding_trader = FundingTrader(bybit_trader, telegram_send)
#
#   # Nel job queue (ogni 60 secondi):
#   async def trading_job(context):
#       symbols = await bybit_client.get_top_symbols(50)  # top 50 per volume
#       await funding_trader.run_loop(
#           symbols        = symbols,
#           chat_id        = OWNER_CHAT_ID,
#           get_funding_fn = bybit_client.get_funding_rate,
#           interval       = 60
#       )
#
#   # Comando /stats nel bot:
#   async def cmd_stats(update, context):
#       stats = funding_trader.get_stats()
#       msg = (
#           f"📊 *Statistiche Trading*\n"
#           f"Trade totali: {stats['trades']}\n"
#           f"Win rate: {stats['win_rate']}%\n"
#           f"PnL totale: {stats['total_pnl']:+.2f} USDT\n"
#           f"Media vincita: +{stats['avg_win']:.2f} USDT\n"
#           f"Media perdita: {stats['avg_loss']:.2f} USDT\n"
#           f"Posizioni aperte: {stats['open']}"
#       )
#       await update.message.reply_text(msg, parse_mode="Markdown")
#
# ─────────────────────────────────────────────
