"""
exchanges/ — FundShot SaaS
Exchange clients multi-tenant.
"""

from .models import FundingTicker, Position, WalletBalance, InstrumentInfo, OrderResult
from .base import ExchangeClient
from .bybit import BybitClient

# Registry: exchange_id → classe
EXCHANGE_REGISTRY: dict[str, type[ExchangeClient]] = {
    "bybit": BybitClient,
    # "binance": BinanceClient,        # FASE 2
    # "okx":     OKXClient,            # FASE 3
    # "hyperliquid": HyperliquidClient, # FASE 5
}

SUPPORTED_EXCHANGES = list(EXCHANGE_REGISTRY.keys())


def make_client(
    exchange: str,
    api_key: str = "",
    api_secret: str = "",
    demo: bool = True,
    testnet: bool = False,
    **kwargs,
) -> ExchangeClient:
    """
    Factory: crea il client giusto dato l'exchange ID.

    Esempio:
        client = make_client("bybit", api_key="...", api_secret="...", demo=True)
    """
    cls = EXCHANGE_REGISTRY.get(exchange)
    if cls is None:
        raise ValueError(
            f"Exchange '{exchange}' non supportato. "
            f"Disponibili: {SUPPORTED_EXCHANGES}"
        )
    return cls(api_key=api_key, api_secret=api_secret, demo=demo, testnet=testnet, **kwargs)


__all__ = [
    "ExchangeClient", "BybitClient",
    "FundingTicker", "Position", "WalletBalance", "InstrumentInfo", "OrderResult",
    "EXCHANGE_REGISTRY", "SUPPORTED_EXCHANGES", "make_client",
]
