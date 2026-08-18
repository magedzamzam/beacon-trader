"""Fanout order + the adverse-fill statistic (#211).

The A/B/C fanout placed the same signal on acct5, then acct7, then acct8, in
series, so execution latency was PERFECTLY confounded with arm identity. Over
the frozen window [2026-08-10, 2026-08-15): median lag 4.6s for the second arm
and 8.8s for the third, costing the third $0.62 of adverse entry fill per trade
- 0.058R against a $10.66 median stop. The measured matched dR (B -0.023,
C -0.059) is almost exactly what the lag alone predicts (-0.013, -0.058), and
the arm that sat last all along is the one that kept failing its bar. Nothing
recorded the lag, so none of this was visible.

This is a MEASUREMENT fix, not a strategy one: sizing, risk and whether a trade
is placed are untouched. What changes is which arm draws the short straw.
"""
from collections import Counter

import pytest

from beacon_core.execution.planner import (adverse_fill, adverse_fill_by_account,
                                           fanout_order)

ARMS = [5, 7, 8]


# --- the permutation ----------------------------------------------------------
def test_order_is_not_a_fixed_function_of_account_id():
    """The bug in one line: acct8 was last on every signal, forever."""
    orders = {tuple(fanout_order(ARMS, sig)) for sig in range(1, 200)}
    assert len(orders) > 1


def test_each_account_occupies_each_position_at_a_consistent_rate():
    """Randomisation converts a systematic bias into noise the day-block
    bootstrap already accounts for — but only if it is actually even."""
    pos = Counter()
    n = 600
    for sig in range(1, n + 1):
        for i, acct in enumerate(fanout_order(ARMS, sig)):
            pos[(acct, i)] += 1
    expected = n / len(ARMS)
    for acct in ARMS:
        for i in range(len(ARMS)):
            # generous band: this pins "no arm is pinned to a slot", not the RNG
            assert 0.8 * expected < pos[(acct, i)] < 1.2 * expected, (acct, i)


def test_the_order_is_reproducible_from_the_signal_alone():
    """A re-drive must place in the order the original run did, and a reviewer
    months later must be able to reconstruct it without it having been stored."""
    for sig in (1, 42, 1249, 99999):
        assert fanout_order(ARMS, sig) == fanout_order(ARMS, sig)


def test_the_callers_row_order_cannot_leak_into_the_permutation():
    """The incoming list is a DB row order that an unrelated edit can change; if
    it fed the shuffle, the sequence would stop being reproducible."""
    for sig in (1, 42, 1249):
        assert fanout_order([5, 7, 8], sig) == fanout_order([8, 5, 7], sig)


def test_every_account_is_placed_exactly_once():
    """A permutation, not a sample: dropping an arm would be a missing trade."""
    for sig in range(1, 50):
        out = fanout_order(ARMS, sig)
        assert sorted(out) == sorted(ARMS) and len(out) == len(set(out))


@pytest.mark.parametrize("ids,sig", [([], 1), ([5], 1), ([5, 7], None)])
def test_degenerate_inputs_are_returned_untouched(ids, sig):
    assert fanout_order(ids, sig) == list(ids)


# --- the statistic ------------------------------------------------------------
def test_adverse_fill_is_signed_by_direction():
    """A BUY that fills HIGHER and a SELL that fills LOWER are both worse, so
    this cannot be a plain subtraction."""
    assert adverse_fill(2000.0, 2000.62, "BUY") == pytest.approx(0.62)
    assert adverse_fill(2000.0, 1999.38, "SELL") == pytest.approx(0.62)
    assert adverse_fill(2000.0, 1999.38, "BUY") == pytest.approx(-0.62)
    assert adverse_fill(2000.0, 2000.62, "SELL") == pytest.approx(-0.62)


@pytest.mark.parametrize("c,a", [(None, 2000.0), (2000.0, None), ("x", 2000.0)])
def test_an_unfilled_arm_says_nothing_about_execution_quality(c, a):
    assert adverse_fill(c, a, "BUY") is None


def test_the_per_account_summary_reproduces_the_shape_of_the_finding():
    """acct8 fills worse than acct7 and both fill worse than the control, which
    is the whole finding: the handicap is monotone in queue position."""
    rows = []
    for sig in range(1, 21):
        rows += [
            {"signal_id": sig, "account_id": 5, "direction": "BUY",
             "entry_fill": 2000.0, "placement_lag_ms": 0},
            {"signal_id": sig, "account_id": 7, "direction": "BUY",
             "entry_fill": 2000.14, "placement_lag_ms": 4600},
            {"signal_id": sig, "account_id": 8, "direction": "BUY",
             "entry_fill": 2000.62, "placement_lag_ms": 8800},
        ]
    out = adverse_fill_by_account(rows, control_account=5)
    assert set(out) == {7, 8}                     # the control is not its own arm
    assert out[7]["mean_adverse_fill"] == pytest.approx(0.14)
    assert out[8]["mean_adverse_fill"] == pytest.approx(0.62)
    assert out[8]["mean_adverse_fill"] > out[7]["mean_adverse_fill"]
    assert out[8]["median_lag_ms"] == 8800 and out[8]["n"] == 20
    assert out[7]["worse_fill_share"] == 1.0


def test_a_signal_the_control_never_traded_is_dropped_not_scored():
    """No control fill, no comparison — counting it as zero would drag the mean
    toward "no handicap", which is the claim under test."""
    rows = [{"signal_id": 1, "account_id": 8, "direction": "BUY",
             "entry_fill": 2000.62, "placement_lag_ms": 8800},
            {"signal_id": 2, "account_id": 5, "direction": "BUY",
             "entry_fill": 2000.0, "placement_lag_ms": 0},
            {"signal_id": 2, "account_id": 8, "direction": "BUY",
             "entry_fill": 2000.5, "placement_lag_ms": 9000}]
    out = adverse_fill_by_account(rows, control_account=5)
    assert out[8]["n"] == 1 and out[8]["mean_adverse_fill"] == pytest.approx(0.5)


def test_a_lag_is_summarised_even_when_the_arm_did_not_fill():
    """The two are separate evidence: an arm can be late AND not fill, and the
    lag is still what the weekly asserts on."""
    rows = [{"signal_id": 1, "account_id": 5, "direction": "BUY",
             "entry_fill": 2000.0, "placement_lag_ms": 0},
            {"signal_id": 1, "account_id": 8, "direction": "BUY",
             "entry_fill": None, "placement_lag_ms": 9000}]
    out = adverse_fill_by_account(rows, control_account=5)
    assert out[8]["n"] == 0 and out[8]["mean_adverse_fill"] is None
    assert out[8]["median_lag_ms"] == 9000


def test_the_summary_is_empty_rather_than_wrong_with_no_rows():
    assert adverse_fill_by_account([], control_account=5) == {}
