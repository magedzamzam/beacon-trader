"""Symmetric encryption for secrets stored in the database.

Broker credentials and AI keys can now be entered from the frontend and stored
in the DB rather than only referenced from .env. To keep them out of plaintext,
they are Fernet-encrypted with a key derived from the `SECRET_KEY` env var.

Envelope format: a stored ciphertext is the string `enc:v1:<token>` so the
reader can tell an encrypted value from a legacy plaintext one and refuse to
mis-handle it. `decrypt` is a no-op on anything without the prefix.
"""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

_PREFIX = "enc:v1:"


class CryptoError(Exception):
    pass


@lru_cache
def _fernet() -> Fernet:
    """Derive a stable Fernet key from SECRET_KEY (any-length string).

    We hash the configured secret to exactly 32 bytes and urlsafe-base64 it,
    which is the shape Fernet requires — so operators can use any passphrase.
    """
    secret = get_settings().secret_key
    if not secret:
        raise CryptoError(
            "SECRET_KEY is not set — cannot encrypt/decrypt stored secrets. "
            "Set a long random SECRET_KEY in the environment."
        )
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def has_key() -> bool:
    return bool(get_settings().secret_key)


def is_encrypted(value: Optional[str]) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


def encrypt(plaintext: str) -> str:
    token = _fernet().encrypt((plaintext or "").encode("utf-8")).decode("ascii")
    return f"{_PREFIX}{token}"


def decrypt(value: Optional[str]) -> Optional[str]:
    """Decrypt an `enc:v1:` value; pass through anything else unchanged."""
    if not is_encrypted(value):
        return value
    token = value[len(_PREFIX):]
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:  # wrong SECRET_KEY, or corrupted value
        raise CryptoError(
            "Failed to decrypt a stored secret — SECRET_KEY may have changed."
        ) from exc
