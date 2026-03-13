"""
ws_liquidations.py — FundShot Bot
Monitoraggio liquidazioni grandi via WebSocket pubblico Bybit.
"""

import asyncio
import json
import logging
import os
from typing import Callable, Awaitable

import aiohttp

logger = logging.getLogger(__name__)

LIQUIDATION_MIN_USD = float(os.getenv("LIQUIDATION_MIN_USD", 100_000))

# WebSocket pubblico Bybit V5 (mainnet)
WS_URL = "wss://stream.bybit.com/v5/public/linear"

# Set simboli da monitorare (se vuoto → nessun WS attivo)
_watched_symbols: set[str] = set()


def set_watched_symbols(symbols: set[str]):
    global _watched_symbols
    _watched_symbols = symbols


async def run_liquidation_ws(
    alert_callback: Callable[[str], Awaitable[None]],
    symbols: list[str] | None = None,
):
    """
    Connette al WebSocket Bybit e monitora le liquidazioni.
    Chiama alert_callback(message) per ogni liquidazione >= LIQUIDATION_MIN_USD.
    """
    if not symbols:
        logger.info("WS Liquidazioni: nessun simbolo configurato, skip.")
        return

    # Bybit permette max ~100 simboli per connessione
    topics = [f"liquidation.{s}" for s in symbols[:100]]
    subscribe_msg = json.dumps({"op": "subscribe", "args": topics})

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(WS_URL, heartbeat=20) as ws:
                    logger.info("WS Liquidazioni connesso. Simboli: %d", len(symbols))
                    await ws.send_str(subscribe_msg)

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                await _process_liquidation(data, alert_callback)
                            except Exception as e:
                                logger.debug("WS parse error: %s", e)
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            logger.warning("WS chiuso o errore, riconnetto...")
                            break

        except Exception as e:
            logger.error("WS Liquidazioni errore: %s — riprovo in 10s", e)
            await asyncio.sleep(10)


async def _process_liquidation(data: dict, callback: Callable[[str], Awaitable[None]]):
    """Analizza il messaggio WebSocket e chiama callback se rilevante."""
    topic = data.get("topic", "")
    if not topic.startswith("liquidation."):
        return

    d = data.get("data", {})
    symbol = d.get("symbol", "")
    side = d.get("side", "")         # Buy (long liquidato) | Sell (short liquidato)
    size = float(d.get("size", 0))
    price = float(d.get("price", 0))
    usd_value = size * price

    if usd_value < LIQUIDATION_MIN_USD:
        return

    from alert_logic import format_liquidation_alert
    msg = format_liquidation_alert(symbol, side, size, usd_value)
    logger.info("Liquidazione alert: %s", msg)
    await callback(msg)
