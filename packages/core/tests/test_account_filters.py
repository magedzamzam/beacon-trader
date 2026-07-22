"""Account-scoping predicates for the Trading read APIs (#118).

The global account selector must actually scope Positions / History / Activity /
Chart. These pin the two predicates the `trades` and `events` routers apply:

* ``account_id is None`` ("All accounts") is a no-op — the query is returned
  untouched, so the selector defaults to showing everything.
* with an account, trades filter on ``trades.account_id`` directly, and events
  filter via their trade (events carry no account of their own).
"""
import re

from sqlalchemy import select

from beacon_core.db.filters import (scope_events_to_account,
                                     scope_trades_to_account)
from beacon_core.db.models import Event, Trade


def _sql(q) -> str:
    return str(q.compile(compile_kwargs={"literal_binds": True}))


def test_trades_all_accounts_is_noop():
    q = select(Trade)
    # None must leave the query untouched (same object) — "All accounts".
    assert scope_trades_to_account(q, None) is q


def test_trades_scoped_to_account():
    sql = _sql(scope_trades_to_account(select(Trade), 7))
    assert re.search(r"WHERE trades\.account_id = 7", sql), sql


def test_events_all_accounts_is_noop():
    q = select(Event)
    assert scope_events_to_account(q, None) is q


def test_events_scoped_via_trade_join():
    # Events reference an account only through their trade, so the predicate must
    # be a subquery over trades owned by the account — not a bogus events.account_id.
    sql = _sql(scope_events_to_account(select(Event), 7))
    assert "events.account_id" not in sql, "events have no account_id column"
    assert re.search(r"events\.trade_id IN \(SELECT trades\.id", sql), sql
    assert re.search(r"WHERE trades\.account_id = 7", sql), sql


def test_events_account_filter_composes_with_other_predicates():
    # The router applies kind/leg filters before the account scope; both survive.
    q = scope_events_to_account(select(Event).where(Event.kind == "placed"), 7)
    sql = _sql(q)
    assert "events.kind = 'placed'" in sql, sql
    assert "trades.account_id = 7" in sql, sql
