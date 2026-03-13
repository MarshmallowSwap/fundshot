"""
exchanges/base.py — FundShot SaaS
Interfaccia astratta comune a tutti gli exchange.
Ogni implementazione (Bybit, Binance, OKX, Hyperliquid) eredita da questa.
"""

from abc import ABC, abstractmethod
from typing import Optional
from .models import (
    FundingTicker, Position, WalletBalance,
    InstrumentInfo, OrderResult,
)


class ExchangeClient(ABC):
    """
    Interfaccia astratta per tutti gli exchange supportati.

    Ogni exchange implementa:
      - Monitoring: get_funding_tickers, get_funding_history, get_instruments_info
      - Account:    get_wallet_balance, get_positions
      - Trading:    open_position, close_position, set_trailing_stop, set_sl_tp
      - Info:       get_mark_price, get_instrument_info
      - Diagnostica: test_connection
    """

    EXCHANGE_ID: str = ""   # 'bybit' | 'binance' | 'okx' | 'hyperliquid'

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        demo: bool = True,
        testnet: bool = False,
        **kwargs,
    ):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.demo       = demo
        self.testnet    = testnet

    # ── MONITORING ────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_funding_tickers(self) -> list[FundingTicker]:
        """
        Restituisce tutti i ticker perpetual USDT con funding rate != 0.
        Deve essere efficiente (una o poche chiamate API).
        """
        ...

    @abstractmethod
    async def get_funding_history(
        self, symbol: str, limit: int = 8
    ) -> list[dict]:
        """Storico funding rate per un simbolo (ultimi `limit` cicli)."""
        ...

    @abstractmethod
    async def get_funding_history_7d(self, symbol: str) -> list[dict]:
        """Storico funding rate degli ultimi 7 giorni."""
        ...

    @abstractmethod
    async def get_instruments_info(self) -> dict[str, InstrumentInfo]:
        """
        Info statica su tutti i simboli (funding interval, cap, qty step).
        Usato per classificare i simboli e calcolare le qty.
        """
        ...

    # ── ACCOUNT ───────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_wallet_balance(self) -> Optional[WalletBalance]:
        """Saldo del conto (equity, balance disponibile, PnL)."""
        ...

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Tutte le posizioni aperte con size > 0."""
        ...

    # ── TRADING ───────────────────────────────────────────────────────────────

    @abstractmethod
    async def open_position(
        self,
        symbol: str,
        side: str,           # 'Buy' | 'Sell'
        qty: float,
        leverage: int,
        sl_pct: float = 0.0,
        tp_pct: float = 0.0,
    ) -> OrderResult:
        """Apre una posizione market con SL/TP opzionali."""
        ...

    @abstractmethod
    async def close_position(
        self, symbol: str, side: str, size: float
    ) -> OrderResult:
        """Chiude una posizione esistente con ordine market."""
        ...

    @abstractmethod
    async def set_trailing_stop(
        self,
        symbol: str,
        trailing_pct: float,
        active_price: Optional[float] = None,
    ) -> bool:
        """
        Imposta trailing stop nativo (se supportato dall'exchange).
        Se non supportato, l'implementazione deve emularlo.
        """
        ...

    @abstractmethod
    async def set_sl_tp(
        self,
        symbol: str,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
    ) -> bool:
        """Aggiorna Stop Loss e/o Take Profit su una posizione aperta."""
        ...

    # ── INFO ──────────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_mark_price(self, symbol: str) -> float:
        """Restituisce il mark price corrente per un simbolo."""
        ...

    @abstractmethod
    async def get_instrument_info(
        self, symbol: str
    ) -> Optional[InstrumentInfo]:
        """Info su un singolo simbolo."""
        ...

    # ── DIAGNOSTICA ───────────────────────────────────────────────────────────

    @abstractmethod
    async def test_connection(self) -> dict:
        """
        Testa la connessione all'exchange.
        Restituisce: { 'public': {...}, 'auth': {...}, 'positions': {...} }
        """
        ...

    # ── UTILITY COMUNE ────────────────────────────────────────────────────────

    @staticmethod
    def _sf(val, default: float = 0.0) -> float:
        """Safe float: converte stringhe vuote e None senza eccezione."""
        try:
            return float(val) if val not in (None, "", "—") else default
        except (TypeError, ValueError):
            return default

    def __repr__(self) -> str:
        env = "testnet" if self.testnet else ("demo" if self.demo else "live")
        return f"{self.__class__.__name__}({env})"


    # ── METODI EXTRA (con default) ────────────────────────────────────────────

    async def get_open_interest(self, symbol: str) -> Optional[dict]:
        """
        OI corrente e variazione 5m/10m.
        Restituisce: { 'oi': float, 'change_5m': float, 'change_10m': float }
        Default: None (exchange che non lo supportano ritornano None).
        """
        return None

    async def calc_qty(
        self, symbol: str, size_usdt: float, leverage: int
    ) -> Optional[float]:
        """
        Calcola la qty da ordinare rispettando minOrderQty e qtyStep.
        Usa get_mark_price + get_instrument_info.
        """
        import math
        price = await self.get_mark_price(symbol)
        if not price:
            return None
        info = await self.get_instrument_info(symbol)
        if not info:
            return None
        step  = info.qty_step  or 0.001
        min_q = info.min_order_qty or 0.001
        notional = size_usdt * leverage
        raw_qty  = notional / price
        decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
        qty = round(math.floor(raw_qty / step) * step, decimals)
        qty = max(qty, min_q)
        return qty
