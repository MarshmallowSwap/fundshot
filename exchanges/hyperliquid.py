"""
exchanges/hyperliquid.py — FundShot
Client Hyperliquid per funding rates e trading.

API: https://api.hyperliquid.xyz
- Pubblica: POST /info (no auth)
- Privata: POST /exchange (firma EIP-712 con wallet ETH)

Funding: per-hour (intervallo 1h fisso)
Simboli: BTC, ETH, SOL, etc. (senza USDT suffix)
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

from .models import FundingTicker, Position, WalletBalance, InstrumentInfo

logger = logging.getLogger(__name__)

BASE_URL = "https://api.hyperliquid.xyz"
EXCHANGE_ID = "hyperliquid"


class HyperliquidClient:
    """
    Client Hyperliquid — supporta solo market data per ora.
    Trading richiede wallet ETH (EIP-712) — da implementare.
    """

    EXCHANGE_ID = EXCHANGE_ID

    def __init__(self, api_key: str = "", api_secret: str = "",
                 demo: bool = False, testnet: bool = False, **kwargs):
        self.api_key    = api_key     # wallet address ETH (per trading)
        self.api_secret = api_secret  # private key ETH (per trading)
        self.demo       = demo
        # Hyperliquid non ha testnet separato — usa mainnet
        logger.info("HyperliquidClient init — mainnet only")

    async def _post(self, path: str, payload: dict) -> dict:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                BASE_URL + path,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                return await r.json()

    async def get_funding_tickers(self) -> list[FundingTicker]:
        """Fetch tutti i funding rates da Hyperliquid."""
        try:
            data = await self._post("/info", {"type": "metaAndAssetCtxs"})
            if not isinstance(data, list) or len(data) < 2:
                logger.error("HyperliquidClient: risposta inattesa %s", str(data)[:100])
                return []

            assets = data[0].get("universe", [])
            ctxs   = data[1]
            tickers = []

            for asset, ctx in zip(assets, ctxs):
                name = asset.get("name", "")
                if not name:
                    continue
                # Hyperliquid usa formato senza USDT (es. "BTC" non "BTCUSDT")
                # Ma nella nostra dashboard usiamo BTCUSDT — aggiungiamo il suffix
                symbol = name + "USDT"
                try:
                    funding_rate = float(ctx.get("funding", 0))
                    mark_price   = float(ctx.get("markPx", 0))
                    open_interest = float(ctx.get("openInterest", 0))
                except (ValueError, TypeError):
                    continue

                if abs(funding_rate) < 0.00001:
                    continue

                tickers.append(FundingTicker(
                    symbol=symbol,
                    funding_rate=funding_rate,      # già in decimale (es. 0.001 = 0.1%)
                    next_funding_time=0,             # HL non fornisce next funding time
                    funding_interval_h=1,            # HL usa intervalli di 1 ora
                    last_price=mark_price,
                    price_24h_pct=0.0,
                    exchange=EXCHANGE_ID,
                ))

            logger.info("HyperliquidClient: %d tickers", len(tickers))
            return tickers

        except Exception as e:
            logger.error("HyperliquidClient get_funding_tickers: %s", e)
            return []

    async def get_positions(self) -> list[Position]:
        """Richiede API key (wallet ETH) — non ancora implementato."""
        if not self.api_key:
            return []
        try:
            data = await self._post("/info", {
                "type": "clearinghouseState",
                "user": self.api_key,
            })
            positions = []
            for p in data.get("assetPositions", []):
                pos = p.get("position", {})
                szi = float(pos.get("szi", 0))
                if szi == 0:
                    continue
                coin   = pos.get("coin", "")
                symbol = coin + "USDT"
                entry  = float(pos.get("entryPx", 0))
                unr    = float(pos.get("unrealizedPnl", 0))
                lev    = float(pos.get("leverage", {}).get("value", 1))
                liq    = float(pos.get("liquidationPx", 0) or 0)
                side   = "Buy" if szi > 0 else "Sell"
                positions.append(Position(
                    symbol=symbol,
                    side=side,
                    size=abs(szi),
                    avg_price=entry,
                    mark_price=entry,   # non disponibile qui
                    leverage=lev,
                    unrealised_pnl=unr,
                    pnl_pct=0.0,
                    position_im=0.0,
                    liq_price=liq,
                    take_profit=0.0,
                    stop_loss=0.0,
                    cur_realised_pnl=0.0,
                    exchange=EXCHANGE_ID,
                ))
            return positions
        except Exception as e:
            logger.error("HyperliquidClient get_positions: %s", e)
            return []

    async def get_wallet_balance(self) -> Optional[WalletBalance]:
        """Richiede wallet address ETH."""
        if not self.api_key:
            return None
        try:
            data = await self._post("/info", {
                "type": "clearinghouseState",
                "user": self.api_key,
            })
            margin = data.get("marginSummary", {})
            equity   = float(margin.get("accountValue", 0))
            margin_u = float(margin.get("totalMarginUsed", 0))
            avail    = equity - margin_u
            unr      = float(margin.get("totalUnrealizedPnl", 0))
            return WalletBalance(
                total_equity=equity,
                total_wallet_balance=equity,
                total_available_balance=avail,
                total_perp_upl=unr,
                total_margin_balance=equity,
                coins=[],
                exchange=EXCHANGE_ID,
            )
        except Exception as e:
            logger.error("HyperliquidClient get_wallet_balance: %s", e)
            return None

    # ── Trading — placeholder (richiede EIP-712) ─────────────────────────────
    def get_mark_price(self, symbol: str) -> Optional[float]:
        logger.warning("HyperliquidClient.get_mark_price: not implemented for sync calls")
        return None

    def calc_qty(self, symbol: str, size_usdt: float, leverage: int) -> Optional[float]:
        logger.warning("HyperliquidClient.calc_qty: not implemented")
        return None

    def place_order(self, *args, **kwargs) -> Optional[str]:
        logger.warning("HyperliquidClient.place_order: trading not yet implemented")
        return None

    def set_trailing_stop(self, *args, **kwargs) -> bool:
        return False

    def close_position(self, *args, **kwargs) -> bool:
        logger.warning("HyperliquidClient.close_position: trading not yet implemented")
        return False

    def get_position(self, symbol: str) -> Optional[dict]:
        return None

    def get_open_interest(self, symbol: str) -> Optional[dict]:
        return None

    async def get_instruments_info(self, symbol: str = "") -> list[InstrumentInfo]:
        return []
