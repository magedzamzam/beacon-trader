"""A 404 on /confirms/{dealReference} is a timing artifact, not a rejection (#150).

Drives CapitalComAdapter.place_order over a stubbed `_request` so no session,
network, or credentials are involved.
"""
import asyncio
import datetime as dt
from decimal import Decimal

from beacon_core.brokers.capital_com import CapitalComAdapter
from beacon_core.brokers.types import (
    BrokerOrder, BrokerPosition, Direction, NotFoundError, OrderSide,
    OrderStatus, OrderType, PlaceOrderRequest,
)


def _market_req(qty="0.5"):
    return PlaceOrderRequest(
        broker_symbol="GOLD", side=OrderSide.BUY, order_type=OrderType.MARKET,
        quantity=Decimal(qty), stop_loss=Decimal("4000"),
    )


def _limit_req(qty="0.5", level="4045"):
    return PlaceOrderRequest(
        broker_symbol="GOLD", side=OrderSide.SELL, order_type=OrderType.LIMIT,
        quantity=Decimal(qty), limit_price=Decimal(level),
    )


def _adapter(confirm_seq, **stubs):
    """Adapter whose `_request` answers POST with a dealReference and serves
    `confirm_seq` (exceptions raised, dicts returned) to the confirm polls."""
    a = CapitalComAdapter(credentials={"api_key": "k", "account_username": "u",
                                       "account_password": "p"})
    calls = {"confirms": 0}

    async def fake_request(method, path, json=None, params=None, _retry_on_401=True):
        if method == "POST":
            return {"dealReference": "o_deadbeef"}
        if path.startswith("/api/v1/confirms/"):
            item = confirm_seq[min(calls["confirms"], len(confirm_seq) - 1)]
            calls["confirms"] += 1
            if isinstance(item, BaseException):
                raise item
            return item
        raise AssertionError(f"unexpected {method} {path}")

    a._request = fake_request
    for name, fn in stubs.items():
        setattr(a, name, fn)
    return a, calls


def _run(coro_fn):
    """Run without real backoff sleeps."""
    orig_sleep = asyncio.sleep
    asyncio.sleep = lambda *_: orig_sleep(0)
    try:
        return asyncio.run(coro_fn())
    finally:
        asyncio.sleep = orig_sleep


_ACCEPTED = {"dealStatus": "ACCEPTED", "dealId": "d1", "level": "4043.4",
             "size": "0.5", "affectedDeals": [{"dealId": "pos1", "status": "OPENED"}]}


def test_transient_404_then_confirm_fills_the_leg():
    # The whole point of #150: two 404s then a real confirm must NOT reject.
    a, calls = _adapter([NotFoundError("404"), NotFoundError("404"), _ACCEPTED])
    order = _run(lambda: a.place_order(_market_req()))
    assert order.status == OrderStatus.FILLED
    assert order.broker_order_ref == "pos1"
    assert order.fill_price == Decimal("4043.4")
    assert calls["confirms"] == 3          # it retried rather than giving up


def test_confirm_retry_is_bounded():
    a, calls = _adapter([NotFoundError("404")],
                        list_positions=lambda: _empty(), list_orders=lambda: _empty())
    try:
        _run(lambda: a.place_order(_market_req()))
        raise AssertionError("expected NotFoundError")
    except NotFoundError:
        pass
    assert calls["confirms"] == CapitalComAdapter._CONFIRM_MAX_ATTEMPTS


async def _empty():
    return []


def test_a_real_rejection_is_still_a_rejection():
    # A confirm that answers REJECTED is terminal — retries must not paper over it.
    a, calls = _adapter([{"dealStatus": "REJECTED", "reason": "RISK_CHECK"}])
    order = _run(lambda: a.place_order(_market_req()))
    assert order.status == OrderStatus.REJECTED
    assert order.rejection_reason == "RISK_CHECK"
    assert calls["confirms"] == 1


def _position(ref="pos9", qty="0.5", direction=Direction.LONG, age_s=1):
    opened = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(seconds=age_s)).replace(tzinfo=None)
    return BrokerPosition(
        broker_symbol="GOLD", broker_position_ref=ref, quantity=Decimal(qty),
        avg_open_price=Decimal("4043.4"), direction=direction, opened_at=opened,
    )


def test_persistent_404_adopts_an_unambiguous_position():
    # The deal exists at the broker despite the 404 — adopting it beats failing
    # the leg and leaving an unmonitored position behind.
    async def positions():
        return [_position()]

    a, _ = _adapter([NotFoundError("404")], list_positions=positions)
    order = _run(lambda: a.place_order(_market_req()))
    assert order.status == OrderStatus.FILLED
    assert order.broker_order_ref == "pos9"
    assert order.raw["reconciled_from"] == "o_deadbeef"


def test_persistent_404_does_not_adopt_when_ambiguous():
    # Two same-epic/size/direction legs in the window: we cannot tell which is
    # ours, so fail the leg rather than bind two legs to one broker position.
    async def positions():
        return [_position(ref="posA"), _position(ref="posB")]

    a, _ = _adapter([NotFoundError("404")], list_positions=positions)
    try:
        _run(lambda: a.place_order(_market_req()))
        raise AssertionError("expected NotFoundError")
    except NotFoundError:
        pass


def test_persistent_404_ignores_a_pre_existing_position():
    # Opened well before we sent the request -> not ours.
    async def positions():
        return [_position(age_s=600)]

    a, _ = _adapter([NotFoundError("404")], list_positions=positions)
    try:
        _run(lambda: a.place_order(_market_req()))
        raise AssertionError("expected NotFoundError")
    except NotFoundError:
        pass


def test_persistent_404_adopts_an_unambiguous_working_order():
    created = (dt.datetime.now(dt.timezone.utc)).isoformat()

    async def orders(status=None):
        return [BrokerOrder(
            broker_order_ref="wo1", broker_symbol="GOLD", side=OrderSide.SELL,
            order_type=OrderType.LIMIT, quantity=Decimal("0.5"),
            limit_price=Decimal("4045"), status=OrderStatus.WORKING,
            raw={"workingOrderData": {"createdDateUTC": created}},
        )]

    a, _ = _adapter([NotFoundError("404")], list_orders=orders)
    order = _run(lambda: a.place_order(_limit_req()))
    assert order.status == OrderStatus.WORKING
    assert order.broker_order_ref == "wo1"


def test_persistent_404_ignores_a_working_order_with_no_timestamp():
    # Without a creation time we cannot place it in our window — an older,
    # already-tracked order would otherwise be adopted twice.
    async def orders(status=None):
        return [BrokerOrder(
            broker_order_ref="wo_old", broker_symbol="GOLD", side=OrderSide.SELL,
            order_type=OrderType.LIMIT, quantity=Decimal("0.5"),
            limit_price=Decimal("4045"), status=OrderStatus.WORKING, raw={},
        )]

    a, _ = _adapter([NotFoundError("404")], list_orders=orders)
    try:
        _run(lambda: a.place_order(_limit_req()))
        raise AssertionError("expected NotFoundError")
    except NotFoundError:
        pass
