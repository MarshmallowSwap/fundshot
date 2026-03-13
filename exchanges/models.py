"""
exchanges/models.py — Funding King SaaS
Dataclass condivisi tra tutti gli exchange client.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FundingTicker:
    """Snapshot funding rate per un simbolo."""
    symbol: str
    funding_rate: float          # es. 0.01234 = 1.234%
    next_funding_time: int       # timestamp ms
    funding_interval_h: float    # ore tra settlement (es. 8.0)
    last_price: float
    price_24h_pct: float         # variazione 24h (es. -0.032 = -3.2%)
    prev_price_1h: float = 0.0
    open_interest: float = 0.0
    exchange: str = ""           # 'bybit' | 'binance' | 'okx' | 'hyperliquid'


@dataclass
class Position:
    """Posizione aperta su un exchange."""
    symbol: str
    side: str                    # 'Buy' | 'Sell'
    size: float
    avg_price: float
    mark_price: float
    leverage: float
    unrealised_pnl: float
    pnl_pct: float
    position_im: float           # margin iniziale
    liq_price: float
    take_profit: float
    stop_loss: float
    cur_realised_pnl: float
    exchange: str = ""


@dataclass
class WalletBalance:
    """Saldo account."""
    total_equity: float
    total_wallet_balance: float
    total_available_balance: float
    total_perp_upl: float
    total_margin_balance: float
    coins: list = field(default_factory=list)
    exchange: str = ""


@dataclass
class InstrumentInfo:
    """Info statica su un simbolo."""
    symbol: str
    funding_interval_min: int    # minuti (es. 480 = 8H)
    upper_funding_rate: float
    lower_funding_rate: float
    min_order_qty: float = 0.0
    qty_step: float = 0.0
    exchange: str = ""


@dataclass
class OrderResult:
    """Risultato apertura/chiusura ordine."""
    ok: bool
    order_id: str = ""
    symbol: str = ""
    side: str = ""
    qty: float = 0.0
    price: float = 0.0
    error: str = ""
    exchange: str = ""
