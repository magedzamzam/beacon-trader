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

    Broker secrets come ONLY from the database — entered in the portal and
    stored Fernet-encrypted. We deliberately do NOT read broker credentials
    from the environment/.env:
      * `<name>_enc`  -> Fernet-decrypt `<name>` (the supported way to store a
                          secret; see beacon_core.crypto)
      * `<name>_env`  -> IGNORED (legacy). The secret is NOT read from the
                          environment; re-enter it in the portal so it is stored
                          encrypted in the DB.
      * anything else -> passed through literally (e.g. `is_demo`)

    Example ref (the only supported shape now):
        {"api_key_enc": "enc:v1:...", "account_username_enc": "enc:v1:...",
         "account_password_enc": "enc:v1:...", "is_demo": false}
    """
    from ..crypto import decrypt
    from ..logging import get_logger

    out: dict = {}
    legacy_env_keys: list[str] = []
    for k, v in (ref or {}).items():
        if k.endswith("_env"):
            # Legacy .env reference — no longer honoured. Skip it so no secret
            # is ever pulled from the environment.
            legacy_env_keys.append(k)
            continue
        if k.endswith("_enc"):
            out[k[:-4]] = decrypt(v) or ""
        else:
            out[k] = v

    if legacy_env_keys:
        get_logger("brokers").warning(
            "Broker credentials_ref uses legacy .env references %s which are no "
            "longer read from the environment — enter the credentials in the "
            "portal so they are stored encrypted in the DB.",
            legacy_env_keys,
        )
    return out
