"""Ruling a filter arm on its removed set (#186).

Arm C's week-6 false positive (an arm that risked 27% as much reading as +0.108R
of skill) had a twin on Arm B, and it is the opposite mistake: the `adx_regime`
filter ran for four days without firing once, so "no difference between the arms"
was really "the filter was never tested". Both errors are ways of ruling on a
number that measures something other than what it is being read as, and both are
pinned here by name.
"""
from __future__ import annotations

import pytest

from beacon_core.analysis.report import (
    FILTER_ACCUMULATE, FILTER_NO_EVIDENCE, FILTER_REMOVES_LOSERS,
    FILTER_REMOVES_WINNERS, FILTER_UNTESTED, MIN_REMOVED_N, filter_removed_set)

BASE = 0.6


def _skips(sigs, epoch="adx_regime@1h"):
    return [{"signal_id": s, "epoch": epoch} for s in sigs]


def _control(sigs, *, pl, risk=100.0):
    """Control-arm trades for `sigs`. `pl` is a scalar or a per-signal list."""
    vals = pl if isinstance(pl, (list, tuple)) else [pl] * len(sigs)
    return [{"signal_id": s, "realized_pl": v, "planned_risk": risk}
            for s, v in zip(sigs, vals)]


def _one(out, epoch="adx_regime@1h"):
    return out["epochs"][epoch]


# --- the untested/null distinction -------------------------------------------
def test_a_filter_that_never_fired_is_untested_not_null():
    """Four days of Arm B with zero skips. It was a literal duplicate of the
    control, and reporting that as "the filter has no edge" is the error #186
    was filed on."""
    out = filter_removed_set(_skips([], epoch="adx_regime@4h"),
                             _control([1, 2, 3], pl=-50.0), base_rate=BASE)
    assert out["epochs"] == {}          # nothing fired, so there is no verdict
    assert out["n_epochs"] == 0


def test_an_epoch_that_fired_once_is_reported_rather_than_swallowed():
    out = filter_removed_set(_skips([1]), _control([1], pl=-50.0), base_rate=BASE)
    e = _one(out)
    assert e["n_skipped"] == 1
    assert e["verdict"] == FILTER_ACCUMULATE


# --- epochs are never pooled --------------------------------------------------
def test_a_timeframe_change_splits_the_arm_into_two_experiments():
    """The 4h half fired 0 times and the 1h half 8. Averaging them describes no
    filter that ever ran, so there is deliberately no pooled figure."""
    skips = _skips([1, 2], epoch="adx_regime@4h") + _skips([3, 4, 5])
    out = filter_removed_set(skips, _control([1, 2, 3, 4, 5], pl=-50.0),
                             base_rate=BASE)
    assert set(out["epochs"]) == {"adx_regime@4h", "adx_regime@1h"}
    assert out["epochs"]["adx_regime@4h"]["n_skipped"] == 2
    assert out["epochs"]["adx_regime@1h"]["n_skipped"] == 3
    assert "pooled" not in out


def test_the_return_carries_no_across_epoch_total_to_reach_for():
    skips = _skips([1], epoch="a") + _skips([2], epoch="b")
    out = filter_removed_set(skips, _control([1, 2], pl=-50.0), base_rate=BASE)
    assert set(out) == {"epochs", "n_epochs", "min_n", "note"}


# --- one test, at accumulated N ----------------------------------------------
def test_below_the_floor_the_verdict_is_accumulate_and_not_a_reading():
    sigs = list(range(MIN_REMOVED_N - 1))
    out = filter_removed_set(_skips(sigs), _control(sigs, pl=-50.0),
                             base_rate=BASE)
    e = _one(out)
    assert e["verdict"] == FILTER_ACCUMULATE
    assert "test once" in e["reason"]
    # The interval is still computed — hiding it just gets it recomputed by hand.
    assert e["win_rate_ci"][0] is not None


def test_the_same_signal_skipped_on_several_legs_counts_once():
    """One signal fans out to several legs and each logs its own
    `entry_filtered` event. Counting events would inflate N straight past the
    floor the whole discipline rests on."""
    out = filter_removed_set(_skips([1, 1, 1, 2]),
                             _control([1, 2], pl=-50.0), base_rate=BASE)
    assert _one(out)["n_skipped"] == 2


def test_the_note_states_the_peeking_rule_beside_the_numbers():
    out = filter_removed_set(_skips([1]), _control([1], pl=-50.0), base_rate=BASE)
    assert "peeking" in out["note"] and "once" in out["note"]


# --- the verdicts -------------------------------------------------------------
def _n(k):
    return list(range(k))


def test_a_removed_set_that_lost_on_the_control_says_the_filter_works():
    sigs = _n(40)
    out = filter_removed_set(_skips(sigs), _control(sigs, pl=-50.0),
                             base_rate=BASE)
    e = _one(out)
    assert e["verdict"] == FILTER_REMOVES_LOSERS
    assert e["removed_set_net"] == -2000.0
    assert e["win_rate_ci"][1] < BASE          # ruled on the UPPER bound


def test_a_removed_set_that_made_money_says_the_filter_cuts_profit():
    sigs = _n(40)
    out = filter_removed_set(_skips(sigs), _control(sigs, pl=120.0),
                             base_rate=BASE)
    e = _one(out)
    assert e["verdict"] == FILTER_REMOVES_WINNERS
    assert e["win_rate_ci"][0] > BASE


def test_an_interval_spanning_the_base_rate_is_no_evidence():
    sigs = _n(40)
    # 24/40 = 0.60 — exactly the base rate.
    pls = [120.0] * 24 + [-50.0] * 16
    out = filter_removed_set(_skips(sigs), _control(sigs, pl=pls), base_rate=BASE)
    e = _one(out)
    assert e["verdict"] == FILTER_NO_EVIDENCE
    assert e["win_rate_ci"][0] < BASE < e["win_rate_ci"][1]


def test_a_low_win_rate_that_still_made_money_is_not_a_verdict():
    """TP1 distance ranges ~7x across channels, so a removed set can win rarely
    and still be net POSITIVE. Ruling on the win rate alone would have the filter
    cutting profitable signals and reporting itself as protective."""
    sigs = _n(40)
    pls = [900.0] * 8 + [-50.0] * 32          # 20% win rate, net +5,000
    out = filter_removed_set(_skips(sigs), _control(sigs, pl=pls), base_rate=BASE)
    e = _one(out)
    assert e["win_rate_ci"][1] < BASE          # looks protective on win rate...
    assert e["removed_set_net"] > 0            # ...and made the control money
    assert e["verdict"] == FILTER_NO_EVIDENCE
    assert "disagree" in e["reason"]


# --- what cannot be scored ----------------------------------------------------
def test_a_skip_the_control_never_traded_is_a_hole_not_a_flat_removal():
    """The control's own risk guard or a failed fill took the signal too. That
    signal is unscoreable — booking it at 0 would drag the removed set's mean
    toward nothing and quietly shrink the effect."""
    sigs = _n(10)
    out = filter_removed_set(_skips(sigs), _control(sigs[:6], pl=-50.0),
                             base_rate=BASE)
    e = _one(out)
    assert e["n_skipped"] == 10
    assert e["n_scored"] == 6
    assert e["n_unscoreable"] == 4
    assert e["removed_set_net"] == -300.0


def test_near_flat_removals_are_scored_apart_from_losses():
    """Spread and commission push a true breakeven slightly negative; booking
    those as losses biases the base rate every other verdict is read against."""
    sigs = _n(10)
    pls = [-50.0] * 6 + [0.4] * 4
    out = filter_removed_set(_skips(sigs), _control(sigs, pl=pls),
                             base_rate=BASE, eps=1.0)
    e = _one(out)
    assert e["n_flat"] == 4
    assert e["n_decisive"] == 6
    assert e["wins"] == 0


def test_r_is_computed_off_planned_risk_and_skips_rows_without_it():
    sigs = _n(4)
    ct = _control(sigs, pl=-50.0)
    ct[0]["planned_risk"] = None
    ct[1]["planned_risk"] = 0
    out = filter_removed_set(_skips(sigs), ct, base_rate=BASE)
    e = _one(out)
    assert e["n_scored"] == 4                  # the P&L still counts
    assert e["mean_r"] == pytest.approx(-0.5)  # R only where risk is known


def test_no_skips_at_all_returns_an_empty_report_rather_than_failing():
    out = filter_removed_set([], [], base_rate=BASE)
    assert out["epochs"] == {} and out["n_epochs"] == 0
