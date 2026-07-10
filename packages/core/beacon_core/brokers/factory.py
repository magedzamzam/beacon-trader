"""Session-aware broker construction (#36): the single place that turns an
account into a live adapter, plus the shared symbol-map lookup. Both were
duplicated across the executor, monitor, and API routers."""
from __future__ import annotations

from sqlalchemy import select

from ..db.models import Broker, SymbolMap
from .registry import get_adapter, resolve_credentials


async def symbol_map(session, broker_id: int, symbol: str):
    """The SymbolMap row for (broker, internal symbol), or None."""
    return (await session.execute(select(SymbolMap).where(
        SymbolMap.broker_id == broker_id,
        SymbolMap.internal_symbol == symbol))).scalar_one_or_none()


def _broker_creds(broker) -> dict:
    creds = resolve_credentials(broker.credentials_ref)
    creds.setdefault("is_demo", broker.is_demo)
    return creds


def make_adapter(broker):
    """Adapter bound to a broker but NOT a specific account — for connection
    tests and account discovery (`list_accounts`)."""
    return get_adapter(broker.type, _broker_creds(broker))


async def build_adapter(session, account):
    """Resolve the account's broker credentials and return `(broker, adapter)`
    bound to the mapped broker account. The single choke point for adapter
    construction (and any future pool/backoff policy)."""
    broker = await session.get(Broker, account.broker_id)
    creds = _broker_creds(broker)
    creds["account_id"] = account.broker_account_id
    return broker, get_adapter(broker.type, creds)
