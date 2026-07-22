"""Reusable account-scoping predicates for read APIs (#118).

The UI has a global account selector, but the *Trading* pages (Positions,
History, Activity, Chart overlays) used to ignore it. These helpers add the
account filter to trade/event reads so the selector actually scopes those pages.

They live in core (not the FastAPI layer) so they can be unit-tested without the
web stack and reused across routers. Each is a no-op when ``account_id is None``
("All accounts"), so callers can pass the raw query param straight through.
"""
from __future__ import annotations

from sqlalchemy import select

from .models import Event, Trade


def scope_trades_to_account(q, account_id: int | None):
    """Scope a ``Trade`` query to one account (no-op when ``account_id`` is None).

    Trades carry ``account_id`` directly, so this is a plain column predicate.
    """
    if account_id is None:
        return q
    return q.where(Trade.account_id == account_id)


def scope_events_to_account(q, account_id: int | None):
    """Scope an ``Event`` query to one account (no-op when ``account_id`` is None).

    Events reference an account only through ``trade_id``, so filter on the set
    of trades owned by the account. Events with no ``trade_id`` are account-less
    and are therefore excluded once an account is selected — intended: the
    Activity page then shows only that account's execution log.
    """
    if account_id is None:
        return q
    return q.where(
        Event.trade_id.in_(select(Trade.id).where(Trade.account_id == account_id))
    )
