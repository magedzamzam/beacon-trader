"""Broker factory. Maps a broker `type` string to an adapter instance."""
from __future__ import annotations

from typing import Dict, Optional

from .base import BrokerAdapter
from .capital_com import CapitalComAdapter

_REGISTRY = {
    "capital.com": CapitalComAdapter,
    "capital": CapitalComAdapter,
}


def get_adapter(broker_type: str, credentials: Dict,
                display_metadata: Optional[Dict] = None,
                base_url: Optional[str] = None) -> BrokerAdapter:
    key = (broker_type or "").strip().lower()
    cls = _REGISTRY.get(key)
    if cls is None:
        raise ValueError(f"Unknown broker type '{broker_type}'. "
                         f"Known: {sorted(_REGISTRY)}")
    return cls(credentials=credentials, display_metadata=display_metadata,
               base_url=base_url)


def resolve_credentials(ref: dict) -> dict:
    """Turn a stored credentials_ref into live credentials.

    Keys ending in `_env` are read from the environment (secrets stay in .env);
    any other key is passed through literally. Example ref:
        {"api_key_env": "CAP_API_KEY",
         "account_username_env": "CAP_USERNAME",
         "account_password_env": "CAP_PASSWORD",
         "is_demo": true}
    """
    import os
    out: dict = {}
    for k, v in (ref or {}).items():
        if k.endswith("_env"):
            out[k[:-4]] = os.getenv(str(v), "")
        else:
            out[k] = v
    return out
