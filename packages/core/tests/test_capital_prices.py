"""Price history belongs to the adapter, not to a second client (#257).

`scripts/candle_sync.py` hand-rolled a complete Capital REST client — demo/live
hostnames, POST /session, CST + X-SECURITY-TOKEN, 401 re-auth, 429 back-off —
purely because `CapitalComAdapter` had no way to reach `/prices`. That put a
provider's hostnames and session semantics outside the abstraction, and the copy
was strictly weaker than the original it duplicated.

Drives `fetch_candles` over a stubbed `_request`, so no session, network or
credentials are involved.
"""
import asyncio
import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from beacon_core.brokers.base import BrokerAdapter
from beacon_core.brokers.capital_com import CapitalComAdapter
from beacon_core.brokers.types import (
    BrokerCandle, BrokerError, NetworkError, NotFoundError, RateLimitError,
)

REPO = Path(__file__).resolve().parents[3]

BAR = {
    "snapshotTimeUTC": "2026-08-26T12:00:00",
    "openPrice": {"bid": 3900.1, "ask": 3900.5},
    "highPrice": {"bid": 3901.0, "ask": 3901.4},
    "lowPrice": {"bid": 3899.0, "ask": 3899.4},
    "closePrice": {"bid": 3900.8, "ask": 3901.2},
    "lastTradedVolume": 42,
}


def _adapter(seq):
    """Adapter whose `_request` serves `seq` in order (exceptions raised)."""
    a = CapitalComAdapter(credentials={"api_key": "k", "account_username": "u",
                                       "account_password": "p"})
    calls = []

    async def fake_request(method, path, json=None, params=None, _retry_on_401=True):
        calls.append((method, path, dict(params or {})))
        item = seq[min(len(calls) - 1, len(seq) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    a._request = fake_request
    return a, calls


def _run(coro_fn):
    """Run without real backoff sleeps."""
    orig = asyncio.sleep
    asyncio.sleep = lambda *_: orig(0)
    try:
        return asyncio.run(coro_fn())
    finally:
        asyncio.sleep = orig


FRM = dt.datetime(2026, 8, 26, 12, 0, tzinfo=dt.timezone.utc)
TO = dt.datetime(2026, 8, 26, 20, 0, tzinfo=dt.timezone.utc)


def test_a_prices_payload_parses_into_rows():
    a, calls = _adapter([{"prices": [BAR]}])
    out = _run(lambda: a.fetch_candles("GOLD", "MINUTE", FRM, TO))
    assert len(out) == 1
    c = out[0]
    assert isinstance(c, BrokerCandle)
    assert c.ts == FRM
    assert (c.open_bid, c.open_ask) == (Decimal("3900.1"), Decimal("3900.5"))
    assert (c.high_bid, c.high_ask) == (Decimal("3901.0"), Decimal("3901.4"))
    assert (c.low_bid, c.low_ask) == (Decimal("3899.0"), Decimal("3899.4"))
    assert (c.close_bid, c.close_ask) == (Decimal("3900.8"), Decimal("3901.2"))
    assert c.volume == Decimal("42")
    assert c.raw is BAR          # the store's own quality policy needs the payload
    method, path, params = calls[0]
    assert (method, path) == ("GET", "/api/v1/prices/GOLD")
    # naive UTC in Capital's own format — an aware datetime is converted, not rejected
    assert params["from"] == "2026-08-26T12:00:00" and params["to"] == "2026-08-26T20:00:00"
    assert params["resolution"] == "MINUTE"


def test_bars_come_back_oldest_first():
    later = dict(BAR, snapshotTimeUTC="2026-08-26T12:05:00")
    a, _ = _adapter([{"prices": [later, BAR]}])
    out = _run(lambda: a.fetch_candles("GOLD", "MINUTE", FRM, TO))
    assert [c.ts.minute for c in out] == [0, 5]


def test_a_bar_with_no_timestamp_is_not_a_bar():
    """Dropped rather than passed on with a None ts, which would only fail later
    inside the store's insert."""
    a, _ = _adapter([{"prices": [{"openPrice": {"bid": 1}}, BAR]}])
    assert len(_run(lambda: a.fetch_candles("GOLD", "MINUTE", FRM, TO))) == 1


def test_an_empty_window_is_a_normal_answer_and_is_not_retried():
    """Capital 404s a window with no bars in it, which is every window over a
    weekend. Retrying that four times only burns rate limit — the judgement the
    script's own client had already reached, kept."""
    a, calls = _adapter([NotFoundError("no bars")])
    assert _run(lambda: a.fetch_candles("GOLD", "MINUTE", FRM, TO)) == []
    assert len(calls) == 1


def test_no_prices_key_is_an_empty_list_not_a_crash():
    a, _ = _adapter([{}])
    assert _run(lambda: a.fetch_candles("GOLD", "MINUTE", FRM, TO)) == []


def test_a_rate_limit_is_retried_then_raised():
    a, calls = _adapter([RateLimitError("429")])
    with pytest.raises(RateLimitError):
        _run(lambda: a.fetch_candles("GOLD", "MINUTE", FRM, TO))
    assert len(calls) == CapitalComAdapter._PRICES_MAX_RETRIES


def test_a_transient_network_error_recovers():
    a, calls = _adapter([NetworkError("boom"), {"prices": [BAR]}])
    out = _run(lambda: a.fetch_candles("GOLD", "MINUTE", FRM, TO))
    assert len(out) == 1 and len(calls) == 2


def test_an_unknown_resolution_is_refused_before_the_request():
    a, calls = _adapter([{"prices": []}])
    with pytest.raises(BrokerError):
        _run(lambda: a.fetch_candles("GOLD", "EVERY_FORTNIGHT", FRM, TO))
    assert calls == []


def test_naive_datetimes_pass_through_unchanged():
    a, calls = _adapter([{"prices": []}])
    _run(lambda: a.fetch_candles("GOLD", "MINUTE",
                                 dt.datetime(2026, 8, 26, 12, 0),
                                 dt.datetime(2026, 8, 26, 20, 0)))
    assert calls[0][2]["from"] == "2026-08-26T12:00:00"


def test_the_interface_carries_it_but_does_not_force_a_stub():
    """An adapter for a broker with no price-history endpoint is still a usable
    adapter; making this abstract would say otherwise. It is on the ABC so the
    next fetcher reaches for it instead of writing a third session client."""
    assert hasattr(BrokerAdapter, "fetch_candles")
    assert "fetch_candles" not in getattr(BrokerAdapter, "__abstractmethods__", set())

    class Bare(BrokerAdapter):                 # noqa: D401 - minimal stand-in
        async def healthcheck(self): return {}
        async def get_account_info(self): return None
        async def list_positions(self): return []
        async def list_orders(self, status=None): return []
        async def place_order(self, req): return None
        async def cancel_order(self, ref): return True
        async def modify_position(self, req): return None
        async def modify_order(self, req): return None
        async def close_position(self, ref, quantity=None): return None
        async def search_instrument(self, query): return []
        async def get_quote(self, sym): return None

    with pytest.raises(NotImplementedError):
        _run(lambda: Bare().fetch_candles("X", "MINUTE", FRM, TO))


def test_the_provider_host_lives_only_under_brokers():
    """The acceptance criterion, as a test. `backend-capital.com` outside
    `beacon_core/brokers/` is a provider-agnosticism break, and it re-appeared
    once already."""
    offenders = []
    for path in list(REPO.glob("scripts/*.py")) + list(REPO.glob("services/**/*.py")):
        if "__pycache__" in str(path):
            continue
        if "backend-capital.com" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == [], offenders


def test_candle_sync_has_no_second_session_client():
    src = (REPO / "scripts/candle_sync.py").read_text(encoding="utf-8")
    assert "class Cap:" not in src
    for leak in ("X-SECURITY-TOKEN", "/session", "urllib.request", "X-CAP-API-KEY"):
        assert leak not in src, f"candle_sync still hand-rolls the session: {leak}"
    # and it goes through the registry, by the broker's own type
    assert "get_adapter(" in src and "fetch_candles(" in src
    # the bar-quality policy stays with the STORE, not with the adapter
    assert "ck_candle_hl" in src and "ck_candle_oc" in src
