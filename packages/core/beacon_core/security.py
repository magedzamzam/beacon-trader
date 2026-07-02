"""Password hashing and stateless session tokens (stdlib only).

Used by the portal's user-login module so the API key no longer has to be pasted
into every browser: users authenticate with username/password and receive a
signed bearer token (HMAC over SECRET_KEY) that the API accepts like the master
API_TOKEN. No external crypto dependency — pbkdf2 + HMAC from hashlib/hmac.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

from .config import get_settings

_ITERATIONS = 200_000
_ALGO = "sha256"


# ---- password hashing -----------------------------------------------------
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_{_ALGO}${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


# ---- signed session tokens ------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload_b64: str) -> str:
    key = get_settings().secret_key.encode("utf-8")
    return _b64(hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest())


def make_token(username: str, ttl_seconds: int = 7 * 24 * 3600) -> str:
    payload = _b64(json.dumps({"sub": username, "exp": int(time.time()) + ttl_seconds}).encode())
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str) -> Optional[str]:
    """Return the username if the token is valid and unexpired, else None."""
    try:
        payload_b64, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        return None
    if not hmac.compare_digest(sig, _sign(payload_b64)):
        return None
    try:
        data = json.loads(_unb64(payload_b64))
    except (ValueError, TypeError):
        return None
    if int(data.get("exp", 0)) < int(time.time()):
        return None
    return data.get("sub")
