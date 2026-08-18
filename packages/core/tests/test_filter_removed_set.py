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
    FILTER_REMOVES_WINNERS, FILTER_UNTESTED, MIN_REMOVED_N,
    day_block_bootstrap_diff, filter_removed_set, removed_vs_kept_expectancy)

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


# --- the expectancy criterion (#212) -----------------------------------------
# The win-rate arm cannot fire on this book. At payoff_ratio 0.427 a bucket
# losing -0.21R per trade still wins ~55% of the time, so its posterior upper
# bound sits ABOVE the base rate while the money screams. Both live epochs
# landed in that hole at N well past the floor, on the first frozen window the
# experiment ever produced — the estimator was specified for losses that come
# from frequency, and this book's come from magnitude.

# Payoff geometry of the real control book, so these fixtures fail the way the
# live data failed rather than the way a hand-picked one would.
AVG_WIN_R, AVG_LOSS_R = 0.312, -0.732
LIVE_BASE = 0.6161
BOOT = dict(n_boot=200, seed=20260801)      # small + seeded: the ruling is fixed


def _book(n, *, wins, win_r, loss_r, first_id, risk=100.0, days=4, channels=4):
    """`n` control trades with EXACTLY `wins` winners spread evenly over the
    days and channels, so a leave-one-out fold drops a representative slice
    rather than an accident of ordering."""
    out = []
    for i in range(n):
        won = (i * wins) // n < ((i + 1) * wins) // n
        out.append({
            "signal_id": first_id + i,
            "realized_pl": risk * (win_r if won else loss_r),
            "planned_risk": risk,
            "day": f"2026-08-{10 + i % days:02d}",
            "source_id": 100 + i % channels,
        })
    return out


def _split(removed, kept, epoch="adx_regime@1h+min_adx30"):
    """(skips, control_trades) for a removed set and the set that was kept."""
    return ([{"signal_id": t["signal_id"], "epoch": epoch} for t in removed],
            removed + kept)


def test_a_magnitude_driven_removed_set_fires_on_expectancy_not_win_rate():
    """Arm B, as it actually read: 53 decisive removals, -0.21R, and a win-rate
    interval that spans the base rate because the losses are big, not frequent.
    The old estimator returned NO_EVIDENCE on the only robust effect in the
    experiment."""
    removed = _book(53, wins=29, win_r=AVG_WIN_R, loss_r=AVG_LOSS_R, first_id=1)
    kept = _book(79, wins=49, win_r=0.55, loss_r=-0.35, first_id=1000)
    skips, control = _split(removed, kept)
    e = _one(filter_removed_set(skips, control, base_rate=LIVE_BASE, **BOOT),
             "adx_regime@1h+min_adx30")

    # the win-rate arm is silent: the upper bound is ABOVE the base rate
    assert e["win_rate_ci"][1] > LIVE_BASE
    assert e["removed_set_net"] < 0 and e["mean_r"] < -0.15
    # ...and the expectancy arm rules
    assert e["verdict"] == FILTER_REMOVES_LOSERS
    assert e["criterion"] == "expectancy"
    assert e["expectancy_dR"] < 0
    lo, hi = e["expectancy_ci"]
    assert lo is not None and hi < 0                  # interval excludes zero
    assert e["n_blocks"] == 4
    assert e["loco_sign_stable"]["same_sign"] == e["loco_sign_stable"]["folds"] == 4
    assert e["lodo_sign_stable"]["same_sign"] == e["lodo_sign_stable"]["folds"] == 4
    assert "win rate cannot separate" in e["reason"]


def test_a_frequency_driven_removed_set_still_fires_on_the_win_rate():
    """The case the original estimator was specified for: the removed set loses
    by losing OFTEN. That arm keeps ruling, and says so."""
    removed = _book(40, wins=10, win_r=1.0, loss_r=-1.0, first_id=1)
    kept = _book(60, wins=40, win_r=1.0, loss_r=-1.0, first_id=1000)
    skips, control = _split(removed, kept)
    e = _one(filter_removed_set(skips, control, base_rate=BASE, **BOOT),
             "adx_regime@1h+min_adx30")
    assert e["verdict"] == FILTER_REMOVES_LOSERS
    assert e["criterion"] == "win_rate"
    assert e["win_rate_ci"][1] < BASE                 # the bound that matters
    assert "upper bound is below the base" in e["reason"]


def test_two_criteria_pointing_opposite_ways_is_not_a_promotion():
    """A removed set that wins constantly and nets positive AED, but on far more
    risk than the kept set — high win rate, negligible R. The win-rate arm reads
    REMOVES_WINNERS, the expectancy arm reads REMOVES_LOSERS, and a disagreement
    is not a verdict."""
    removed = _book(40, wins=34, win_r=0.01, loss_r=-0.02, first_id=1, risk=1000.0)
    kept = _book(60, wins=36, win_r=1.2, loss_r=-0.6, first_id=1000, risk=100.0)
    skips, control = _split(removed, kept)
    e = _one(filter_removed_set(skips, control, base_rate=BASE, **BOOT),
             "adx_regime@1h+min_adx30")
    assert e["win_rate_ci"][0] > BASE and e["removed_set_net"] > 0
    assert e["expectancy_dR"] < 0
    assert e["verdict"] == FILTER_NO_EVIDENCE
    assert e["criterion"] is None
    assert "disagree" in e["reason"]


def test_an_effect_carried_by_one_channel_does_not_survive_the_loco_family():
    """#215's mined suite: -0.2088 with a clean interval one week, and -0.0044
    the moment TFXC was dropped. The whole effect was ever one channel, and the
    fold family is what says so."""
    # channel 100 is a disaster; the other three are indistinguishable from kept
    removed, kept = [], []
    for i in range(60):
        ch, day = 100 + i % 4, f"2026-08-{10 + i % 4:02d}"
        r = -0.9 if ch == 100 else 0.10
        removed.append({"signal_id": i, "realized_pl": 100.0 * r,
                        "planned_risk": 100.0, "day": day, "source_id": ch})
    for i in range(60):
        kept.append({"signal_id": 1000 + i, "realized_pl": 5.0,
                     "planned_risk": 100.0,
                     "day": f"2026-08-{10 + i % 4:02d}", "source_id": 100 + i % 4})
    skips, control = _split(removed, kept)
    e = _one(filter_removed_set(skips, control, base_rate=BASE, **BOOT),
             "adx_regime@1h+min_adx30")
    assert e["expectancy_dR"] < 0                      # pooled, it looks real
    loco = e["loco_sign_stable"]
    assert loco["same_sign"] < loco["folds"]           # ...one fold flips it
    assert e["verdict"] != FILTER_REMOVES_LOSERS       # so it does not promote
    assert e["criterion"] is None


def test_below_the_floor_the_expectancy_arm_offers_no_verdict_either():
    """One test, at accumulated N — the new statistic changes what is measured,
    not the peeking rule."""
    removed = _book(10, wins=5, win_r=AVG_WIN_R, loss_r=AVG_LOSS_R, first_id=1)
    kept = _book(60, wins=40, win_r=0.55, loss_r=-0.35, first_id=1000)
    skips, control = _split(removed, kept)
    e = _one(filter_removed_set(skips, control, base_rate=LIVE_BASE, **BOOT),
             "adx_regime@1h+min_adx30")
    assert e["n_decisive"] < MIN_REMOVED_N
    assert e["verdict"] == FILTER_ACCUMULATE and e["criterion"] is None
    # the number is still reported — hiding it just gets it recomputed by hand
    assert e["expectancy_dR"] is not None


def test_without_a_day_on_the_control_trades_the_old_arm_rules_alone():
    """No day, no blocks, no expectancy — and an epoch is never ruled on a
    statistic that could not be computed."""
    sigs = list(range(1, 41))
    out = filter_removed_set(_skips(sigs), _control(sigs, pl=-50.0),
                             base_rate=BASE, **BOOT)
    e = _one(out)
    assert e["expectancy_dR"] is None and e["expectancy_ci"] == (None, None)
    assert e["loco_sign_stable"] is None and e["lodo_sign_stable"] is None
    assert e["n_blocks"] == 0
    assert e["verdict"] == FILTER_REMOVES_LOSERS and e["criterion"] == "win_rate"


def test_a_single_day_interval_is_degenerate_and_cannot_promote():
    """One block has zero between-block variance, so the interval collapses to a
    point. It must not be readable as tight (the trap #186 was filed on)."""
    removed = _book(40, wins=22, win_r=AVG_WIN_R, loss_r=AVG_LOSS_R,
                    first_id=1, days=1)
    kept = _book(60, wins=40, win_r=0.55, loss_r=-0.35, first_id=1000, days=1)
    skips, control = _split(removed, kept)
    e = _one(filter_removed_set(skips, control, base_rate=LIVE_BASE, **BOOT),
             "adx_regime@1h+min_adx30")
    assert e["n_blocks"] == 1
    assert e["verdict"] != FILTER_REMOVES_LOSERS and e["criterion"] is None


# --- the difference bootstrap itself ------------------------------------------
def test_the_difference_bootstrap_is_seeded_and_reports_its_blocks():
    by_day = {f"2026-08-1{d}": [(-0.5, True), (-0.4, True), (0.3, False),
                                (0.4, False)] for d in range(4)}
    a = day_block_bootstrap_diff(by_day, n_boot=200, seed=7)
    b = day_block_bootstrap_diff(by_day, n_boot=200, seed=7)
    assert a == b                                      # a ruling that moves is not a ruling
    assert a["n_blocks"] == 4 and a["degenerate"] is False
    assert a["mean"] == round(-0.45 - 0.35, 4)
    assert a["ci_high"] < 0


def test_the_difference_bootstrap_needs_both_sides():
    only_removed = {"2026-08-10": [(-0.5, True)], "2026-08-11": [(-0.4, True)]}
    out = day_block_bootstrap_diff(only_removed, n_boot=50, seed=7)
    assert out["mean"] is None and out["degenerate"] is True


def test_the_expectancy_helper_is_usable_on_its_own():
    """It is in the library so the weekly stops hand-rolling it — which is the
    failure mode #186 was created to end."""
    rows = [{"r": -0.5 if i % 2 else -0.6, "day": f"2026-08-1{i % 4}",
             "channel": f"src{i % 3}", "removed": True} for i in range(30)]
    rows += [{"r": 0.2, "day": f"2026-08-1{i % 4}", "channel": f"src{i % 3}",
              "removed": False} for i in range(30)]
    out = removed_vs_kept_expectancy(rows, n_boot=200, seed=7)
    assert out["dR"] < 0 and out["excludes_zero"] is True
    assert out["n_removed"] == 30 and out["n_kept"] == 30
    assert out["loco"]["folds"] == 3 and out["lodo"]["folds"] == 4
