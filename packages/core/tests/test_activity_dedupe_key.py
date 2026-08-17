"""The activity dedup key, pinned to shapes that actually occur (#216).

The previous key was `(account_id, deal_id, activity_at, type)` and it never
fired on live data — not once in 4,353 rows. The test that covered it passed
because it used the SAME `activity_at` for both rows of the duplicate pair,
which is the one thing production never does. So every case here carries a real
observed timestamp gap.
"""
import datetime as dt
from decimal import Decimal

from beacon_core.analysis import broker_truth as BT


def _act(deal, at, pl, source="TP", typ="POSITION", leg=1, trade=1, acct=5):
    return {"account_id": acct, "deal_id": deal, "leg_id": leg, "trade_id": trade,
            "type": typ, "source": source, "realized_pl": pl, "activity_at": at}


def _at(*a):
    return dt.datetime(*a, tzinfo=dt.timezone.utc)


# --- the duplicates the old key could not see -------------------------------

def test_two_feeds_seconds_apart_are_one_close():
    """leg 709: USER at 08:27:06.338, TP at 08:27:14.326, both +25.89.
    Eight seconds apart, so the old timestamp key kept both."""
    rows = [_act("d1", _at(2026, 7, 13, 8, 27, 6, 338000), 25.89, source="USER"),
            _act("d1", _at(2026, 7, 13, 8, 27, 14, 326000), 25.89, source="TP")]
    assert BT.realized_by_trade(rows) == {1: 25.89}


def test_two_feeds_TWO_HOURS_apart_are_still_one_close():
    """The widest observed gap: 2h01m, SL+USER, -200.07. This is why no
    tolerance window can separate a duplicate from a partial close."""
    rows = [_act("d2", _at(2026, 8, 14, 10, 0, 0), -200.07, source="SL"),
            _act("d2", _at(2026, 8, 14, 12, 1, 3, 978000), -200.07, source="USER")]
    assert BT.realized_by_trade(rows) == {1: -200.07}


def test_the_SAME_feed_repeating_itself_is_one_close():
    """6 groups are TP+TP, 45ms to 1.26s apart (legs 5868, 5421, 5748, ...).
    A rule keyed on `source` differing would miss every one of them."""
    rows = [_act("d3", _at(2026, 8, 12, 9, 0, 0, 0), 244.16, source="TP"),
            _act("d3", _at(2026, 8, 12, 9, 0, 1, 262000), 244.16, source="TP")]
    assert BT.realized_by_trade(rows) == {1: 244.16}


def test_float_and_decimal_of_the_same_money_are_one_close():
    """Two feeds need not agree on the type either."""
    rows = [_act("d4", _at(2026, 8, 12, 9, 0, 0), 100.0, source="USER"),
            _act("d4", _at(2026, 8, 12, 9, 0, 5), Decimal("100.00"), source="TP")]
    assert BT.realized_by_trade(rows) == {1: 100.0}


# --- what must still be preserved -------------------------------------------

def test_partial_closes_of_different_sizes_all_count():
    """The laddered book's actual shape: one deal, several closes, different
    money each time. Collapsing these would drop most of the P&L."""
    rows = [_act("d5", _at(2026, 8, 9, 10), 100.0),
            _act("d5", _at(2026, 8, 9, 11), 50.0),
            _act("d5", _at(2026, 8, 9, 12), 25.0)]
    assert BT.realized_by_trade(rows) == {1: 175.0}


def test_same_amount_on_DIFFERENT_legs_is_two_closes():
    """Two rungs of one ladder can settle the same money. They are separate
    closes and the key must keep them — this is the case the leg_id earns."""
    rows = [_act("d6", _at(2026, 8, 9, 10), 40.0, leg=1),
            _act("d6", _at(2026, 8, 9, 10), 40.0, leg=2)]
    assert BT.realized_by_trade(rows) == {1: 80.0}


def test_same_amount_different_deal_is_two_closes():
    rows = [_act("d7", _at(2026, 8, 9, 10), 40.0),
            _act("d8", _at(2026, 8, 9, 10), 40.0)]
    assert BT.realized_by_trade(rows) == {1: 80.0}


def test_rows_with_no_money_are_not_zeroes():
    rows = [_act("d9", _at(2026, 8, 9, 9), None, typ="EDIT_STOP_AND_LIMIT"),
            _act("d9", _at(2026, 8, 9, 10), 100.0)]
    assert BT.realized_by_trade(rows) == {1: 100.0}


def test_dedupe_does_not_depend_on_row_order():
    a = [_act("da", _at(2026, 8, 9, 10), 100.0, source="USER"),
         _act("da", _at(2026, 8, 9, 10, 0, 8), 100.0, source="TP")]
    assert BT.realized_by_trade(a) == BT.realized_by_trade(list(reversed(a)))


def test_the_key_no_longer_depends_on_activity_at():
    """The regression that started it: identical closes at different times must
    collapse, or the dedup is a no-op again."""
    assert "activity_at" not in BT.IDENTITY
    assert "realized_pl" in BT.IDENTITY and "leg_id" in BT.IDENTITY
