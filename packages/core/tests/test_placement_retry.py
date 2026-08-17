"""Recovering a broker-refused leg instead of dropping its size (#221)."""
from decimal import Decimal

from beacon_core.execution.placement import (
    NO_RETRY, RETRY_AS_MARKET, RETRY_CLAMP_TP,
    classify_broker_error, rejection_event, retry_plan)

LIMIT_ERR = "Capital.com 400: {'errorCode': 'error.validation.limit.price'}"
TP_MAX_ERR = "Capital.com 400: {'errorCode': 'error.invalid.takeprofit.maxvalue: 4083.92'}"
TP_MIN_ERR = "Capital.com 400: {'errorCode': 'error.invalid.takeprofit.minvalue: 4033.75'}"


# --- classification ---------------------------------------------------------

def test_classifies_the_two_recoverable_errors():
    assert classify_broker_error(LIMIT_ERR)["kind"] == RETRY_AS_MARKET
    d = classify_broker_error(TP_MAX_ERR)
    assert d["kind"] == RETRY_CLAMP_TP and d["bound"] == Decimal("4083.92")
    assert classify_broker_error(TP_MIN_ERR)["bound"] == Decimal("4033.75")


def test_unknown_errors_are_not_retried():
    for msg in ("market closed", "error.invalid.epic", "", None,
                "Capital.com 400: {'errorCode': 'error.risk.limit'}"):
        assert classify_broker_error(msg)["kind"] is NO_RETRY
        assert retry_plan(msg, side_buy=True, order_type="LIMIT") is None


# --- crossed LIMIT -> MARKET ------------------------------------------------

def test_crossed_sell_limit_retries_at_market():
    """The real 2026-08-17 case: trade 1183, five LIMIT legs at 4410, the fifth
    refused after the market rose to meet it."""
    plan = retry_plan(LIMIT_ERR, side_buy=False, order_type="LIMIT",
                      entry=Decimal("4410"), take_profit=Decimal("4390"))
    assert plan == {"action": RETRY_AS_MARKET, "order_type": "MARKET", "limit_price": None}


def test_crossed_buy_limit_retries_at_market():
    plan = retry_plan(LIMIT_ERR, side_buy=True, order_type="LIMIT",
                      entry=Decimal("4400"), take_profit=Decimal("4420"))
    assert plan["action"] == RETRY_AS_MARKET


def test_a_market_order_is_never_retried_as_market():
    """It cannot have been refused for being priced through the market, so the
    real cause is something else and repeating it would just fail twice."""
    assert retry_plan(LIMIT_ERR, side_buy=True, order_type="MARKET",
                      entry=Decimal("4400")) is None


# --- take-profit bound ------------------------------------------------------

def test_tp_clamped_to_the_bound_the_broker_named():
    plan = retry_plan(TP_MAX_ERR, side_buy=True, order_type="LIMIT",
                      entry=Decimal("4000"), take_profit=Decimal("4200"))
    assert plan == {"action": RETRY_CLAMP_TP, "take_profit": Decimal("4083.92")}


def test_sell_tp_clamped_up_to_the_min_bound():
    plan = retry_plan(TP_MIN_ERR, side_buy=False, order_type="LIMIT",
                      entry=Decimal("4100"), take_profit=Decimal("4000"))
    assert plan == {"action": RETRY_CLAMP_TP, "take_profit": Decimal("4033.75")}


def test_clamp_refused_when_the_bound_lands_the_wrong_side_of_entry():
    """A wrong-sided bound would flip the target through the entry and make the
    leg an instant loss. Fail closed instead."""
    # BUY whose "max" TP bound sits BELOW the entry.
    assert retry_plan(TP_MAX_ERR, side_buy=True, order_type="LIMIT",
                      entry=Decimal("4200"), take_profit=Decimal("4300")) is None
    # SELL whose "min" TP bound sits ABOVE the entry.
    assert retry_plan(TP_MIN_ERR, side_buy=False, order_type="LIMIT",
                      entry=Decimal("4000"), take_profit=Decimal("3900")) is None


def test_clamp_never_lengthens_the_target():
    """The broker states a ceiling on distance. Moving the target FURTHER away
    would be inventing a more ambitious trade than the channel called."""
    # BUY already targeting 4050; bound 4083.92 is further away -> refuse.
    assert retry_plan(TP_MAX_ERR, side_buy=True, order_type="LIMIT",
                      entry=Decimal("4000"), take_profit=Decimal("4050")) is None
    # SELL already targeting 4060; bound 4033.75 is further away -> refuse.
    assert retry_plan(TP_MIN_ERR, side_buy=False, order_type="LIMIT",
                      entry=Decimal("4100"), take_profit=Decimal("4060")) is None


# --- the event that makes the loss visible ----------------------------------

class _Leg:
    id, tp_index, order_type = 6298, 5, "LIMIT"


def test_rejection_event_carries_the_size_that_never_reached_the_broker():
    ev = rejection_event(_Leg(), intended_lot=Decimal("5.43"), error=LIMIT_ERR,
                         retried_as="MARKET", recovered=True)
    assert ev["intended_lot"] == "5.43"          # the number nobody could see before
    assert ev["leg_id"] == 6298 and ev["tp_index"] == 5
    assert ev["retried_as"] == "MARKET" and ev["recovered"] is True
    assert len(ev["error"]) <= 300


def test_rejection_event_records_an_unrecovered_leg_too():
    ev = rejection_event(_Leg(), intended_lot=Decimal("5.43"), error="market closed")
    assert ev["recovered"] is False and ev["retried_as"] is None
