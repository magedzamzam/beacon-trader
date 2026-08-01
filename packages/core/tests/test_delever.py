"""Telling selection skill from de-levering (#188).

The A/B ruling instrument is `R = realized_pl / planned_risk`, and the promote
bar is deliberately high because a type-I error compounds permanently into the
control with no automatic rollback. An arm that does not deploy its planned risk
— which `entry_style="staged"` does by construction — posts a better R in a
losing week with no entry-selection skill whatsoever, and passes every
robustness check the manual specifies while doing it.

This suite pins the three things that make that case fail loudly:

  * DEPLOYED risk is measured in the same units as planned, so `deployed/planned`
    is a ratio of like for like;
  * a pure de-lever of the control is reported as NO_SKILL_DEMONSTRATED, not as
    a winner — the week-6 false positive, as a test;
  * a genuinely selective arm still reads as outside the null, so the guard
    catches confounds rather than everything.

Pure — no DB.
"""
from decimal import Decimal as D

import pytest

from beacon_core.analysis.report import (NO_SKILL, SKILL_POSSIBLE, UNDECIDABLE,
                                         day_block_bootstrap, delever_null,
                                         delever_report, geometry_ab_rollup)
from beacon_core.risk.sizing import (InstrumentSpec, deployed_risk,
                                     deployed_risk_from_planned, risk_units)

INSTR = InstrumentSpec(value_per_point=D("1"))


# --- measuring the deployed side ----------------------------------------------
def test_deployed_risk_uses_the_same_arithmetic_as_planned_risk():
    """`deployed / planned` is only a ratio if both are the same units. `risk_cash`
    is lot * distance * value_per_point / fx, so this must be too."""
    got = deployed_risk([(D("0.5"), D("4010"))], original_sl=D("4000"),
                        instrument=InstrumentSpec(value_per_point=D("2")),
                        fx_factor=D("4"))
    assert got == D("0.5") * D("10") * D("2") / D("4")


def test_an_unfilled_leg_deploys_nothing():
    assert deployed_risk([(D("1"), None)], original_sl=D("4000"), instrument=INSTR) == 0


def test_a_zero_fill_price_is_unknown_not_free():
    """`fill_price = 0` means the broker did not tell us (#159). Treating it as a
    price would book a 4000-point risk."""
    assert deployed_risk([(D("1"), D("0"))], original_sl=D("4000"), instrument=INSTR) == 0


def test_deployed_from_planned_needs_no_fx_because_it_cancels():
    """The monitor records this on its tick loop. An FX lookup there would be a
    broker call on the path that manages open positions."""
    planned = [(D("1"), D("4010"), D("4000")), (D("1"), D("4010"), D("4000"))]
    filled = [(D("1"), D("4010"), D("4000"))]
    assert deployed_risk_from_planned(planned_risk=D("100"),
                                      planned_legs=planned,
                                      filled_legs=filled) == D("50")


def test_nothing_filled_is_zero_but_no_plan_is_none():
    """Different facts. Zero deployed is a measurement; None is the absence of
    one, and the metrics exclude it rather than averaging it in as zero."""
    assert deployed_risk_from_planned(planned_risk=D("100"),
                                      planned_legs=[(D("1"), D("4010"), D("4000"))],
                                      filled_legs=[]) == 0
    assert deployed_risk_from_planned(planned_risk=None, planned_legs=[],
                                      filled_legs=[]) is None
    assert deployed_risk_from_planned(planned_risk=D("100"), planned_legs=[],
                                      filled_legs=[]) is None


def test_risk_units_is_measured_to_the_stop_it_is_given():
    """The caller passes the ORIGINAL stop. `legs.sl` is ratcheted in place, so a
    trade moved to breakeven would otherwise report ~0 deployed risk — flattering
    exactly the arm this measurement exists to catch."""
    original = risk_units([(D("1"), D("4010"), D("4000"))])
    ratcheted = risk_units([(D("1"), D("4010"), D("4010"))])
    assert original == D("10") and ratcheted == 0


# --- the rollup ----------------------------------------------------------------
def _trade(tid, acct, pl, planned, deployed, sig=None, at=None):
    return {"trade_id": tid, "account_id": acct, "account": "acct%s" % acct,
            "realized_pl": pl, "planned_risk": planned, "deployed_risk": deployed,
            "signal_id": sig, "signal_at": at}


def test_the_confound_is_visible_in_one_row():
    """Identical trades; arm C simply risks 26.7%. On PLANNED risk it looks 0.73R
    better. On DEPLOYED risk both are -1.0 — no difference at all."""
    out = geometry_ab_rollup([_trade(1, 5, -100, 100, 100),
                              _trade(2, 8, -26.7, 100, 26.7)], [])
    arms = {a["account_id"]: a for a in out["by_arm"]}
    assert arms[5]["expectancy_R"] == -1.0 and arms[8]["expectancy_R"] == -0.267
    assert arms[5]["avg_R_deployed"] == -1.0 and arms[8]["avg_R_deployed"] == -1.0
    assert arms[8]["deployed_ratio"] == 0.267


def test_a_null_deployed_risk_is_excluded_not_counted_as_zero():
    out = geometry_ab_rollup([_trade(1, 5, -100, 100, None)], [])
    arm = out["by_arm"][0]
    assert arm["n_deployed"] == 0
    assert arm["deployed_ratio"] is None and arm["avg_R_deployed"] is None


def test_the_existing_keys_are_untouched():
    """Downstream weeklies grep these names."""
    out = geometry_ab_rollup([_trade(1, 5, 50, 100, 100)], [])
    for k in ("n_trades", "win_rate", "win_rate_ci", "avg_R", "expectancy_R",
              "payoff_ratio", "profit_factor", "breakeven_leg_rate",
              "pct_winners_reach_tp3", "net_nominal"):
        assert k in out["by_arm"][0], k


# --- the de-lever null ---------------------------------------------------------
def _delever_pairs(ratio=0.267, days=4, per_day=10, skill=0.0, seed=7):
    import random
    rng = random.Random(seed)
    out = []
    for d in range(days):
        for _ in range(per_day):
            r = rng.gauss(-0.17, 0.9)
            arm = r * ratio + (skill if r < 0 else 0.0)
            out.append({"day": "2026-07-%02d" % (27 + d), "control_r": r,
                        "arm_r": arm, "control_deployed": 100.0,
                        "arm_deployed": 100.0 * ratio, "control_win": r > 0})
    return out


def test_a_pure_delever_is_reported_as_no_skill():
    """Week 6, as a test. Arm C's +0.108 dR passed leave-one-day-out,
    leave-one-channel-out and a CI excluding zero — and was entirely reproduced
    by risking 26.7% as much."""
    out = delever_null(_delever_pairs())
    assert out["verdict"] == NO_SKILL
    assert out["deployed_ratio"] == 0.267
    lo, hi = out["delever_null_dR"]["ci_low"], out["delever_null_dR"]["ci_high"]
    assert lo <= out["observed_dR"]["mean"] <= hi


def test_a_pure_delever_shows_symmetric_capture():
    """The corroborating sign: a SELECTING arm deploys less on losers than on
    winners. Equal capture means it is just a smaller control."""
    out = delever_null(_delever_pairs())
    assert out["win_capture"] == out["loss_capture"] == 0.267
    assert out["capture_asymmetry"] == 0.0


def test_a_genuinely_selective_arm_is_not_called_no_skill():
    """The guard has to catch confounds, not everything."""
    out = delever_null(_delever_pairs(skill=0.8))
    assert out["verdict"] == SKILL_POSSIBLE
    assert out["observed_dR"]["mean"] > out["delever_null_dR"]["ci_high"]


def test_a_single_day_block_is_undecidable_not_significant():
    """One block has zero between-block variance and the bootstrap collapses to a
    point — the exact trap that made #186's post-changeover dR unusable."""
    out = delever_null(_delever_pairs(days=1, per_day=15))
    assert out["verdict"] == UNDECIDABLE
    assert out["observed_dR"]["degenerate"] is True


def test_no_matched_pairs_is_undecidable():
    assert delever_null([])["verdict"] == UNDECIDABLE


def test_unmeasured_deployment_cannot_produce_a_verdict():
    """Without the measurement the question is open, not answered."""
    pairs = [dict(p, control_deployed=None, arm_deployed=None)
             for p in _delever_pairs()]
    assert delever_null(pairs)["verdict"] == UNDECIDABLE


def test_the_bootstrap_is_deterministic():
    """A ruling that moves when you re-run it is not a ruling."""
    a = delever_null(_delever_pairs())["observed_dR"]
    b = delever_null(_delever_pairs())["observed_dR"]
    assert (a["mean"], a["ci_low"], a["ci_high"]) == (b["mean"], b["ci_low"], b["ci_high"])


def test_the_bootstrap_resamples_days_not_trades():
    boot = day_block_bootstrap({"d1": [1.0] * 50, "d2": [-1.0] * 50}, n_boot=500)
    assert boot["n"] == 100 and boot["n_blocks"] == 2
    # Two blocks of opposite sign: the interval must span them, which trade-level
    # resampling would not do.
    assert boot["ci_low"] < 0 < boot["ci_high"]


# --- the report layer ----------------------------------------------------------
def test_delever_report_pairs_on_signal_and_names_its_control():
    import datetime as dt
    day = dt.datetime(2026, 7, 27, tzinfo=dt.timezone.utc)
    trades = []
    for i in range(20):
        at = day + dt.timedelta(days=i % 4)
        trades.append(_trade(i, 5, -100, 100, 100, sig=i, at=at))
        trades.append(_trade(100 + i, 8, -26.7, 100, 26.7, sig=i, at=at))
    out = delever_report(trades)
    assert out["control_account_id"] == 5
    assert out["arms"]["8"]["verdict"] == NO_SKILL
    assert out["arms"]["8"]["n"] == 20


def test_a_signal_only_one_arm_traded_is_not_a_pair():
    """An unmatched signal says nothing about the difference between arms."""
    import datetime as dt
    at = dt.datetime(2026, 7, 27, tzinfo=dt.timezone.utc)
    out = delever_report([_trade(1, 5, -100, 100, 100, sig=1, at=at),
                          _trade(2, 8, -26.7, 100, 26.7, sig=2, at=at)])
    assert out["arms"]["8"]["n"] == 0
    assert out["arms"]["8"]["verdict"] == UNDECIDABLE
