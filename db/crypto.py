"""
db/crypto.py — Funding King SaaS
Encryption/decryption AES-256-GCM per API key degli utenti.

Le API key non vengono mai salvate in chiaro.
La chiave di encryption viene letta da env var ENCRYPTION_KEY (32 byte hex).

Generare una chiave:
    python3 -c "import secrets; print(secrets.token_hex(32))"
"""

import base64
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

_NONCE_SIZE = 12  # 96 bit — standard GCM


def _get_key() -> bytes:
    """Legge la chiave AES-256 da env (32 byte = 64 hex chars)."""
    raw = os.getenv("ENCRYPTION_KEY", "")
    if not raw:
        raise EnvironmentError(
            "ENCRYPTION_KEY non impostata nel .env — "
            "genera con: python3 -c \"import secrets; print(secrets.token_hex(32))\""
        )
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        raise ValueError("ENCRYPTION_KEY deve essere una stringa hex valida (64 caratteri)")
    if len(key) != 32:
        raise ValueError(f"ENCRYPTION_KEY deve essere 32 byte (64 hex chars), trovati {len(key)}")
    return key


def encrypt(plaintext: str) -> str:
    """
    Cifra una stringa con AES-256-GCM.
    Restituisce: base64(nonce + ciphertext + tag)
    """
    if not plaintext:
        return ""
    key   = _get_key()
    aesgcm = AESGCM(key)
    nonce  = os.urandom(_NONCE_SIZE)
    ct     = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(nonce + ct).decode()


def decrypt(token: str) -> str:
    """
    Decifra un token prodotto da encrypt().
    Restituisce la stringa originale o "" in caso di errore.
    """
    if not token:
        return ""
    try:
        key    = _get_key()
        aesgcm = AESGCM(key)
        raw    = base64.b64decode(token)
        nonce  = raw[:_NONCE_SIZE]
        ct     = raw[_NONCE_SIZE:]
        return aesgcm.decrypt(nonce, ct, None).decode()
    except Exception as e:
        logger.error("decrypt error: %s", e)
        return ""


def is_encryption_available() -> bool:
    """True se la chiave è configurata e valida."""
    try:
        _get_key()
        return True
    except Exception:
        return False
