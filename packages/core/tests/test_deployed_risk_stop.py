"""Which stop `deployed_risk` is measured against (#264).

`deployed_risk_from_planned` takes `(lot, price, stop)` triples, so the defect
was never in the function — it was in what the monitor handed it. These tests
pin the property the caller has to preserve: the stop must be the one the leg
was PLANNED with, which since `sl_override` (#249) is no longer the signal's.
"""
from decimal import Decimal

import pytest

from beacon_core.risk.sizing import deployed_risk_from_planned, risk_units


# Trade 1391, the case that surfaced it: acct 8, src 6, `sl_distance: 6.0`.
# XAUUSD SELL, single-level entry 4642.20, channel stop 4654.20, capped to
# 4648.20 by the override before the plan was built and the lots were sized.
SIGNAL_SL = Decimal("4654.20")
PLANNED_SL = Decimal("4648.20")
BUDGET = Decimal("981.26")
PLANNED = [(Decimal("18.56"), Decimal("4643.40")),
           (Decimal("29.69"), Decimal("4645.20")),
           (Decimal("14.84"), Decimal("4642.20"))]
FILLED = [(Decimal("18.56"), Decimal("4643.52")),
          (Decimal("29.69"), Decimal("4644.87")),
          (Decimal("14.84"), Decimal("4642.14"))]


def _dep(stop):
    return deployed_risk_from_planned(
        planned_risk=BUDGET,
        planned_legs=[(l, p, stop) for l, p in PLANNED],
        filled_legs=[(l, p, stop) for l, p in FILLED])


def test_the_stop_changes_the_answer_so_it_has_to_be_the_right_one():
    """The two stops are 6 points apart and give materially different numbers.
    If this ever stops being true the fix has become untestable, not unnecessary."""
    wrong, right = _dep(SIGNAL_SL), _dep(PLANNED_SL)
    assert wrong == pytest.approx(Decimal("994.12"), abs=Decimal("0.05"))
    assert right == pytest.approx(Decimal("1012.33"), abs=Decimal("0.05"))
    # NOT a general rule, and the live rows say so: on trade 1391 the wrong
    # stop understated (994 vs 1012), on trade 1389 it OVERstated (693 vs 628).
    # The ratio is a quotient of two sums that both use the stop, so which way it
    # lands depends on where the filled legs sit relative to the unfilled ones.
    # The defect is that the number is wrong, not that it is wrong in a
    # convenient direction.
    assert right != wrong


def test_the_error_grows_with_the_size_of_the_override():
    """Why this is worth a column rather than a tolerance. The further the
    override moves the stop, the more the ratio is taken over the wrong
    distance — so the bias is largest exactly where `sl_distance` is doing the
    most work, which is the arm the measurement exists to judge."""
    seen = []
    for signal_sl in (Decimal("4650"), Decimal("4654.20"), Decimal("4670"),
                      Decimal("4700")):
        seen.append(abs(_dep(PLANNED_SL) - _dep(signal_sl)))
    assert seen == sorted(seen), seen
    assert seen[-1] > seen[0] * 3


def test_a_ratcheted_stop_must_still_not_be_used():
    """The property #188 introduced `signals.sl` to protect, restated against
    the new column: measuring on a stop that has been moved to breakeven reports
    a trade carrying real money as risking almost nothing. `initial_sl` is
    written once at plan time and never updated, which is what keeps this true —
    the test states the consequence so a future 'just use legs.sl' cannot pass."""
    breakeven = [(l, p, p) for l, p in PLANNED]          # stop ratcheted to entry
    assert risk_units(breakeven) == 0
    assert deployed_risk_from_planned(planned_risk=BUDGET,
                                      planned_legs=breakeven,
                                      filled_legs=breakeven) is None


def test_a_partial_fill_is_still_measured():
    """The None above must mean "nothing filled", not "some legs missing" - a
    ladder that filled one rung of three has a real, smaller deployed risk and
    reporting it as unmeasured would drop exactly the rows #264 is about."""
    one = [(PLANNED[0][0], FILLED[0][1], PLANNED_SL)]
    dep = deployed_risk_from_planned(
        planned_risk=BUDGET,
        planned_legs=[(l, p, PLANNED_SL) for l, p in PLANNED],
        filled_legs=one)
    assert dep is not None and 0 < dep < BUDGET
