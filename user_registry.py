"""
user_registry.py — Funding King SaaS
Registry multi-tenant: mantiene in memoria un ExchangeClient
per ogni utente attivo, caricato da Supabase.

Uso in bot.py:
    from user_registry import registry
    await registry.refresh()           # ricarica da Supabase
    clients = registry.all_clients()   # [(chat_id, client), ...]
"""

import asyncio
import logging
from dataclasses import dataclass

from db.supabase_client import get_all_users, get_credentials, User
from exchanges import make_client, ExchangeClient

logger = logging.getLogger(__name__)


@dataclass
class UserClient:
    chat_id: int
    user_id: str
    exchange: str
    environment: str
    client: ExchangeClient


class UserRegistry:
    """
    Mantiene un pool di ExchangeClient attivi, uno per ogni
    coppia (utente, exchange) configurata in Supabase.
    """

    def __init__(self):
        # { (chat_id, exchange) → UserClient }
        self._clients: dict[tuple, UserClient] = {}
        self._lock = asyncio.Lock()

    async def refresh(self) -> int:
        """
        Ricarica tutti gli utenti + credenziali da Supabase.
        Crea nuovi client, rimuove quelli non più attivi.
        Restituisce il numero di client attivi.
        """
        async with self._lock:
            users = await get_all_users()
            new_clients: dict[tuple, UserClient] = {}

            for user in users:
                for exchange in user.active_exchanges:
                    key = (user.chat_id, exchange)
                    # Riusa client esistente se già caricato
                    if key in self._clients:
                        new_clients[key] = self._clients[key]
                        continue
                    # Carica credenziali e crea nuovo client
                    try:
                        cred = await get_credentials(user.id, exchange)
                        if not cred or not cred.api_key:
                            logger.warning(
                                "Credenziali mancanti: user=%s exchange=%s",
                                user.chat_id, exchange,
                            )
                            continue
                        client = make_client(
                            exchange=exchange,
                            api_key=cred.api_key,
                            api_secret=cred.api_secret,
                            demo=(cred.environment == "demo"),
                            testnet=False,
                        )
                        new_clients[key] = UserClient(
                            chat_id=user.chat_id,
                            user_id=user.id,
                            exchange=exchange,
                            environment=cred.environment,
                            client=client,
                        )
                        logger.info(
                            "Client caricato: chat_id=%s exchange=%s env=%s",
                            user.chat_id, exchange, cred.environment,
                        )
                    except Exception as e:
                        logger.error(
                            "Errore caricamento client %s/%s: %s",
                            user.chat_id, exchange, e,
                        )

            self._clients = new_clients
            logger.info("Registry aggiornato: %d client attivi", len(self._clients))
            return len(self._clients)

    def all_clients(self) -> list[UserClient]:
        """Tutti i client attivi."""
        return list(self._clients.values())

    def get_client(self, chat_id: int, exchange: str = "bybit") -> ExchangeClient | None:
        """Restituisce il client per un utente/exchange specifico."""
        uc = self._clients.get((chat_id, exchange))
        return uc.client if uc else None

    def get_user_client(self, chat_id: int, exchange: str = "bybit") -> UserClient | None:
        return self._clients.get((chat_id, exchange))

    def chat_ids(self) -> list[int]:
        """Lista di tutti i chat_id registrati (con almeno un exchange)."""
        return list({uc.chat_id for uc in self._clients.values()})

    def add_client(
        self,
        chat_id: int,
        user_id: str,
        exchange: str,
        environment: str,
        client: ExchangeClient,
    ) -> None:
        """Aggiunge/aggiorna un client in real-time (dopo onboarding)."""
        self._clients[(chat_id, exchange)] = UserClient(
            chat_id=chat_id,
            user_id=user_id,
            exchange=exchange,
            environment=environment,
            client=client,
        )
        logger.info("Client aggiunto live: chat_id=%s exchange=%s", chat_id, exchange)

    def remove_client(self, chat_id: int, exchange: str) -> None:
        """Rimuove un client (dopo deletekeys)."""
        self._clients.pop((chat_id, exchange), None)

    def __len__(self) -> int:
        return len(self._clients)


# Istanza globale — importata da bot.py e commands.py
registry = UserRegistry()
