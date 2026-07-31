"""Which instant the fill-basis excursion window opens on (#182).

Leg rows carry a fill PRICE but no fill TIME, so the clock comes from the
`events` audit trail. Pure — `pick_fill_entry` is the DB-free half of
`_fill_basis` (repo convention: the reduction is unit-testable on a bare box).
"""
import datetime as dt

from beacon_core.analysis.excursion_store import (CLOCK_TRADE, FILL_EVENT_KINDS,
                                                  pick_fill_entry)


def t(minute):
    return dt.datetime(2026, 7, 20, 12, minute, tzinfo=dt.timezone.utc)


def leg(leg_id, fill_price=3400.0, trade_id=7, created=None):
    return {"leg_id": leg_id, "trade_id": trade_id, "fill_price": fill_price,
            "trade_created_at": created if created is not None else t(0)}


def test_a_resting_limit_is_timed_from_its_fill_not_its_placement():
    """The case this exists for: a LIMIT entry placed at 12:00 and hit at 12:45.
    Opening the window at 12:00 credits the signal with 45 minutes of ticks we
    were not in the market for — inventing excursion out of an unfilled order."""
    got = pick_fill_entry([leg(1, created=t(0))], {(1, "filled"): t(45)})
    trade_id, price, at, source = got
    assert (trade_id, price) == (7, 3400.0)
    assert at == t(45) and source == "filled"


def test_clock_precedence_prefers_the_most_precise_record():
    """`filled` (working order became a position) beats a staged deploy, which
    beats a bare placement — first kind present wins, in FILL_EVENT_KINDS order."""
    evs = {(1, "filled"): t(30), (1, "staged_deployed"): t(20), (1, "placed"): t(10)}
    assert pick_fill_entry([leg(1)], evs)[3] == "filled"
    evs.pop((1, "filled"))
    assert pick_fill_entry([leg(1)], evs)[3] == "staged_deployed"
    evs.pop((1, "staged_deployed"))
    assert pick_fill_entry([leg(1)], evs)[3] == "placed"
    assert FILL_EVENT_KINDS == ("filled", "staged_deployed", "placed")


def test_no_fill_event_falls_back_to_trade_creation_and_says_so():
    """A leg with no fill event at all still gets a window — but the row records
    that the clock is the fallback, so a large 'trade' share is visible as a
    caveat instead of passing for a measured fill time."""
    _tid, _px, at, source = pick_fill_entry([leg(1, created=t(3))], {})
    assert at == t(3) and source == CLOCK_TRADE


def test_the_window_opens_on_the_earliest_leg_to_take_a_position():
    """A fanout's legs fill at different times. The excursion starts when the
    FIRST of them put money on the table, not at the lowest leg id."""
    legs = [leg(1), leg(2), leg(3)]
    evs = {(1, "filled"): t(50), (2, "filled"): t(20), (3, "filled"): t(35)}
    _tid, _px, at, _src = pick_fill_entry(legs, evs)
    assert at == t(20)


def test_unknown_fill_price_legs_are_skipped_not_trusted():
    """A stored 0 is an UNKNOWN fill, not a fill at zero (#159) — it must not
    become the entry price, and its (earlier) clock must not open the window."""
    legs = [leg(1, fill_price=0), leg(2, fill_price=None), leg(3, fill_price=3402.5)]
    evs = {(1, "filled"): t(5), (2, "filled"): t(6), (3, "filled"): t(40)}
    got = pick_fill_entry(legs, evs)
    assert got[1] == 3402.5 and got[2] == t(40)


def test_no_usable_leg_means_no_fill_basis():
    assert pick_fill_entry([], {}) is None
    assert pick_fill_entry([leg(1, fill_price=0)], {}) is None
    # a leg with neither a fill event nor a trade timestamp cannot be placed in time
    assert pick_fill_entry([{"leg_id": 1, "trade_id": 7, "fill_price": 3400.0,
                             "trade_created_at": None}], {}) is None
