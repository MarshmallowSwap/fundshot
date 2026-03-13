"""
auth.py — Funding King SaaS
Autenticazione via Telegram Login Widget.

Flusso:
  1. Frontend mostra il Telegram Login Widget (script ufficiale Telegram)
  2. Utente clicca "Login con Telegram" → Telegram manda i dati al callback
  3. Frontend manda i dati a POST /api/auth/telegram
  4. Questo modulo verifica l'HMAC-SHA256 e genera un JWT
  5. Frontend salva il JWT e lo manda in Authorization: Bearer <token>
  6. Proxy verifica il JWT su ogni richiesta protetta

Documentazione:
  https://core.telegram.org/widgets/login#checking-authorization
"""

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# JWT semplice senza librerie esterne (base64url + HMAC-SHA256)
import base64

_JWT_EXPIRY = 86400 * 30  # 30 giorni


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * padding)


def _jwt_secret() -> bytes:
    secret = os.getenv("JWT_SECRET", "")
    if not secret:
        raise EnvironmentError(
            "JWT_SECRET non impostato nel .env — "
            "genera con: python3 -c \"import secrets; print(secrets.token_hex(32))\""
        )
    return secret.encode()


def create_jwt(payload: dict) -> str:
    """Crea un JWT firmato con HMAC-SHA256."""
    header  = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = {**payload, "iat": int(time.time()), "exp": int(time.time()) + _JWT_EXPIRY}
    body    = _b64url_encode(json.dumps(payload).encode())
    sig_input = f"{header}.{body}".encode()
    signature = _b64url_encode(
        hmac.new(_jwt_secret(), sig_input, hashlib.sha256).digest()
    )
    return f"{header}.{body}.{signature}"


def verify_jwt(token: str) -> Optional[dict]:
    """
    Verifica un JWT e restituisce il payload se valido, None se non valido.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header, body, signature = parts
        sig_input = f"{header}.{body}".encode()
        expected  = _b64url_encode(
            hmac.new(_jwt_secret(), sig_input, hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_b64url_decode(body))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception as e:
        logger.debug("verify_jwt error: %s", e)
        return None


def verify_telegram_hash(data: dict) -> bool:
    """
    Verifica l'hash del Telegram Login Widget.

    Telegram invia:
      { id, first_name, last_name, username, photo_url, auth_date, hash }

    Verifica:
      1. data_check_string = campo1=valore1\ncampo2=valore2... (ordinato, senza hash)
      2. secret_key = SHA256(bot_token)
      3. HMAC-SHA256(data_check_string, secret_key) == hash

    https://core.telegram.org/widgets/login#checking-authorization
    """
    bot_token = os.getenv("TELEGRAM_TOKEN", "")
    if not bot_token:
        logger.error("verify_telegram_hash: TELEGRAM_TOKEN non impostato")
        return False

    received_hash = data.get("hash", "")
    if not received_hash:
        return False

    # Controlla che auth_date non sia troppo vecchia (1 ora max)
    auth_date = int(data.get("auth_date", 0))
    if time.time() - auth_date > 3600:
        logger.warning("verify_telegram_hash: auth_date scaduta")
        return False

    # Costruisci data_check_string
    fields = {k: v for k, v in data.items() if k != "hash"}
    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(fields.items())
    )

    # secret_key = SHA256(bot_token) — NON HMAC, SHA256 puro
    secret_key = hashlib.sha256(bot_token.encode()).digest()

    # HMAC-SHA256
    computed = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed, received_hash)


def extract_token_from_header(authorization: str) -> Optional[str]:
    """Estrae il token dall'header Authorization: Bearer <token>."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[7:].strip()
