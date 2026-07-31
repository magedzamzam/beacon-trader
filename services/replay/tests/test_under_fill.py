"""The gate passed, but 76% of its disagreements are two defects (#185).

`sim=expired live=tp_hit` (34 legs, 47%) and `sim=tp_hit live=breakeven`
(21 legs, 29%) pull in OPPOSITE directions, which is the only reason the
headline reads `disagreement_bias: balanced`. They cancel by accident, not by
construction — the ratio depends on the signal mix, so a sweep over one channel
or one session will not enjoy the same offset.

DEFECT A — the simulator under-fills entries. Three candidate causes with three
different fixes, and the counts alone cannot tell them apart:
  * the candle feed's spread being wider than Capital.com's was;
  * the resolved entry TTL, or the replay window;
  * `_expire_working` running before `_fill_working`, so an order whose TTL
    lapsed on a bar it WOULD have filled in is lost.
This suite pins the diagnostic that separates them, and the third candidate is
isolated exactly rather than inferred.

DEFECT B — the ratchet is applied at the end of a bar and takes effect from the
next, so a breakeven live took mid-minute is skipped and the trade runs on to
target. That is a modelling choice with a measured cost, not a bug, so it is now
SELECTABLE (`ratchet_timing`) and the default is unchanged — the gate decides,
not this comment.

Pure and synthetic, like the rest of this suite.
"""
from __future__ import annotations

import datetime as dt

import pytest

from harness import bars as B
from harness import fills as F
from harness import metrics as M
from harness import validate as V
from harness.portfolio import PortfolioSim
from harness.variants import RATCHET_NEXT_BAR, RATCHET_SAME_BAR, build_variant
from conftest import (NO_RATCHET, T0, series, signal, signal_row,
                      sl_rules_be_at, variant_dict)


# --- the shortfall predicate ---------------------------------------------------
def _bar(o, h, l, c, *, spread=0.2, ts=T0):
    from conftest import bar as mkbar
    return mkbar(ts, o, h, l, c, spread=spread)


def test_the_shortfall_is_the_fill_predicate_expressed_as_a_distance():
    """If the two could disagree, the diagnostic would be measuring a different
    question from the one that produced the miss."""
    bar = _bar(4000, 4005, 3995, 4002)
    for direction in ("BUY", "SELL"):
        for order_type in ("LIMIT", "STOP"):
            for level in (3990.0, 3995.0, 4000.0, 4005.0, 4010.0):
                gap = B.entry_shortfall(direction, order_type, level, bar)
                fillable = (B.limit_touched(direction, level, bar)
                            if order_type == "LIMIT"
                            else B.stop_triggered(direction, level, bar))
                assert (gap <= 0) is fillable, (direction, order_type, level, gap)


def test_a_market_order_has_no_shortfall_to_report():
    assert B.entry_shortfall("BUY", "MARKET", 4000.0, _bar(4000, 4005, 3995, 4002)) is None


def test_the_shortfall_is_measured_on_the_side_that_has_to_reach_the_level():
    """A BUY limit needs the ASK down to it; a SELL limit needs the BID up. Using
    the mid would understate the miss by half the spread, which is precisely the
    magnitude the diagnostic is trying to resolve."""
    bar = _bar(4000, 4000, 4000, 4000, spread=1.0)     # bid 3999.5 / ask 4000.5
    assert B.entry_shortfall("BUY", "LIMIT", 4000.0, bar) == pytest.approx(0.5)
    assert B.entry_shortfall("SELL", "LIMIT", 4000.0, bar) == pytest.approx(0.5)


# --- the near-miss is recorded on the leg --------------------------------------
def _run(mids, *, entry=4000.0, sl=3990.0, tps=(4010.0,), ttl=5, **vkw):
    s = series(mids)
    v = build_variant(variant_dict(
        entry_policy={"entry_style": "limit", "ttl_minutes": ttl}, **vkw))
    row = signal_row(signal(entry=entry, sl=sl, tps=tps), at=s[0].ts)
    return s, PortfolioSim(v, s).run([row])


def test_an_order_that_never_reached_its_level_records_how_far_it_came():
    # Price stays well above a BUY limit at 4000 for the whole TTL.
    s, res = _run([4020.0] * 30)
    leg = res.trades[0].legs[0]
    assert leg.fill_price is None
    assert leg.closest_approach is not None and leg.closest_approach > 0
    # 4020 mid, spread 0.2 -> low_ask 4020.1, level 4000 -> ~20.1 points short.
    assert leg.closest_approach == pytest.approx(20.1, abs=1e-6)


def test_an_order_that_filled_records_a_non_positive_shortfall():
    """Which is what makes the filled and unfilled populations comparable."""
    s, res = _run([4020.0, 3999.0] + [4005.0] * 30)
    leg = res.trades[0].legs[0]
    assert leg.fill_price is not None
    assert leg.closest_approach <= 0


def test_a_near_miss_inside_the_spread_is_visible_as_such():
    """The signature that would say 'the candle feed, not the strategy'."""
    s, res = _run([4000.15] * 30)          # low_ask = 4000.25 vs a 4000 limit
    leg = res.trades[0].legs[0]
    assert leg.fill_price is None
    assert 0 < leg.closest_approach < 0.5


# --- the within-bar ordering candidate, isolated -------------------------------
def test_an_order_retired_on_a_bar_it_would_have_filled_in_is_counted():
    """TTL expiry runs before fills, so this order is lost on the bar that would
    have taken it. Counted rather than argued about — it is the cheapest of the
    three candidate causes to eliminate."""
    # ttl=2, so at the bar 3 minutes in the TTL has lapsed; that same bar dips
    # through the 4000 limit.
    s, res = _run([4020.0, 4020.0, 4020.0, 3990.0] + [4020.0] * 10, ttl=2)
    trade = res.trades[0]
    leg = trade.legs[0]
    assert leg.fill_price is None
    assert leg.expired_on_fillable_bar is True
    assert trade.expired_on_fillable_bar == 1


def test_an_order_that_simply_ran_out_of_time_is_not_counted_as_that():
    s, res = _run([4020.0] * 30, ttl=2)
    assert res.trades[0].expired_on_fillable_bar == 0
    assert res.trades[0].legs[0].expired_on_fillable_bar is False


# --- it reaches the run report -------------------------------------------------
def test_the_under_fill_distribution_rides_on_every_variant_report():
    """Not only inside `validate`: a sweep inherits whatever the under-fill is
    doing, and the operator needs it beside the result rather than in a
    diagnostic they have to remember to run."""
    s, res = _run([4020.0] * 30)
    v = build_variant(variant_dict())
    rep = M.variant_report(res, variant=v, series=s)
    uf = rep["caveats"]["under_fill"]
    assert uf["n_never_reached"] == 1
    assert uf["median_miss_points"] == pytest.approx(20.1, abs=1e-6)
    assert uf["n_missed_by_under_0_5_points"] == 0
    assert "n_expired_on_a_fillable_bar" in uf


def test_the_report_says_which_exit_model_the_arm_ran_under():
    """Two variants that differ on it are not comparable, so it belongs on the
    RESULT and not only in the config that produced it."""
    s, res = _run([4020.0] * 30)
    for timing in (RATCHET_NEXT_BAR, RATCHET_SAME_BAR):
        v = build_variant(variant_dict(ratchet_timing=timing))
        rep = M.variant_report(res, variant=v, series=s)
        assert rep["settings"]["ratchet_timing"] == timing


# --- the gate's own block ------------------------------------------------------
def _sim_leg(**kw):
    base = {"signal_id": 1, "account_id": 1, "tp_index": 1, "direction": "BUY",
            "fill_price": None, "outcome": "expired", "order_type": "LIMIT",
            "entry": 4000.0, "closest_approach": None,
            "expired_on_fillable_bar": False}
    base.update(kw)
    return base


def _live_leg(**kw):
    base = {"signal_id": 1, "account_id": 1, "tp_index": 1, "direction": "BUY",
            "fill_price": 4000.0, "outcome": "tp_hit"}
    base.update(kw)
    return base


def test_the_gate_reports_the_legs_it_never_filled_that_live_did():
    out = V.compare([_sim_leg(closest_approach=0.2)], [_live_leg()])
    uf = out["under_fill"]
    assert uf["n_sim_never_filled_live_did"] == 1
    assert uf["median_miss_points"] == pytest.approx(0.2)
    assert uf["live_outcome_of_the_missed"] == {"tp_hit": 1}


def test_misses_inside_a_spread_point_at_the_feed():
    sims = [_sim_leg(signal_id=i, closest_approach=0.1 + i * 0.05) for i in range(6)]
    lives = [_live_leg(signal_id=i) for i in range(6)]
    uf = V.compare(sims, lives)["under_fill"]
    assert "feed-shaped" in uf["verdict"]
    assert uf["n_missed_by_under_0.5_points"] >= 3


def test_large_misses_point_somewhere_else():
    sims = [_sim_leg(signal_id=i, closest_approach=15.0 + i) for i in range(6)]
    lives = [_live_leg(signal_id=i) for i in range(6)]
    uf = V.compare(sims, lives)["under_fill"]
    assert "not spread-shaped" in uf["verdict"]
    assert uf["n_missed_by_under_0.5_points"] == 0


def test_the_within_bar_ordering_cause_is_named_outright_not_inferred():
    sims = [_sim_leg(signal_id=i, closest_approach=0.1,
                     expired_on_fillable_bar=True) for i in range(4)]
    lives = [_live_leg(signal_id=i) for i in range(4)]
    uf = V.compare(sims, lives)["under_fill"]
    assert uf["n_expired_on_a_fillable_bar"] == 4
    assert "within-bar ordering" in uf["verdict"]


def test_a_gate_with_no_under_fill_says_so_rather_than_reporting_nothing():
    uf = V.compare([_sim_leg(fill_price=4000.0, outcome="tp_hit")],
                   [_live_leg()])["under_fill"]
    assert uf["n_sim_never_filled_live_did"] == 0
    assert "none" in uf["verdict"]


def test_a_miss_that_dodged_a_loser_is_recorded_beside_one_that_cost_a_winner():
    """Same defect, opposite effect on the bias term. Pooling them into one count
    is how the headline came to read 'balanced'."""
    sims = [_sim_leg(signal_id=1, closest_approach=0.3),
            _sim_leg(signal_id=2, closest_approach=0.4)]
    lives = [_live_leg(signal_id=1, outcome="tp_hit"),
             _live_leg(signal_id=2, outcome="sl_hit")]
    uf = V.compare(sims, lives)["under_fill"]
    assert uf["live_outcome_of_the_missed"] == {"sl_hit": 1, "tp_hit": 1}


def test_a_zero_shortfall_is_not_averaged_in_as_a_miss():
    """The level WAS reached; something else stopped the fill. Counting it as a
    ~0 miss would drag the median toward 'it was basically touching' and make
    every run look feed-shaped."""
    sims = [_sim_leg(signal_id=1, closest_approach=0.0),
            _sim_leg(signal_id=2, closest_approach=8.0)]
    lives = [_live_leg(signal_id=1), _live_leg(signal_id=2)]
    uf = V.compare(sims, lives)["under_fill"]
    assert uf["n_sim_never_filled_live_did"] == 2
    assert uf["n_with_a_recorded_miss"] == 1
    assert uf["median_miss_points"] == pytest.approx(8.0)


# --- defect B: the ratchet timing is selectable, and the default is unchanged --
BE_AT_TP1 = sl_rules_be_at(1)


def _ratchet_run(timing):
    """One bar reaches TP1 (arming break-even) and, within that SAME bar, retraces
    back through the entry. Live, a monitor polling through the minute moves the
    runner's stop and is then taken out by the retrace; `next_bar` still has the
    original stop and lets the runner reach TP2 on the following bar."""
    s = series([
        4000.0,                                   # signal bar
        (3999.0, 4000.5, 3999.0, 4000.0),         # fills the 4000 BUY limit
        (4000.0, 4011.0, 3998.0, 4000.6),         # TP1 touched, then back through entry
        (4000.5, 4021.0, 4000.5, 4020.0),         # runner reaches TP2 if still open
        4020.0, 4020.0])
    v = build_variant(variant_dict(sl_rules=BE_AT_TP1, ratchet_timing=timing))
    row = signal_row(signal(entry=4000.0, sl=3990.0, tps=(4010.0, 4020.0)),
                     at=s[0].ts)
    return PortfolioSim(v, s).run([row])


def test_the_default_exit_model_is_unchanged():
    """Nothing moves until someone runs the gate both ways. A silent change here
    would re-baseline every stored run against a different simulator."""
    v = build_variant(variant_dict())
    assert v.ratchet_timing == RATCHET_NEXT_BAR
    assert build_variant(variant_dict(ratchet_timing="nonsense")).ratchet_timing \
        == RATCHET_NEXT_BAR


def test_same_bar_timing_takes_the_breakeven_the_next_bar_model_skips():
    """Defect B in one test: identical bars, identical rules, and the two models
    disagree on exactly the `sim=tp_hit` vs `live=breakeven` cell."""
    late = _ratchet_run(RATCHET_NEXT_BAR)
    early = _ratchet_run(RATCHET_SAME_BAR)
    late_outcomes = [l.outcome for l in late.trades[0].legs]
    early_outcomes = [l.outcome for l in early.trades[0].legs]
    assert "breakeven" in early_outcomes
    assert "breakeven" not in late_outcomes
    # ...and the direction is the one #185 predicts: the default is the rosier.
    assert late.trades[0].realized_pl > early.trades[0].realized_pl


def test_the_timing_choice_changes_the_variant_digest():
    """Two runs under different exit models must not share a digest — the
    reproducibility claim is that the same digest reproduces the same results."""
    a = build_variant(variant_dict(name="v", ratchet_timing=RATCHET_NEXT_BAR))
    b = build_variant(variant_dict(name="v", ratchet_timing=RATCHET_SAME_BAR))
    assert a.digest() != b.digest()


def test_the_gate_can_be_run_both_ways_without_editing_the_baseline():
    """The baseline config is GENERATED from the live tables so nobody
    hand-edits it. Settling defect B means running the gate under both models, so
    the override has to be a flag — and it has to beat a timing the baseline
    already states, or the operator compares a run against itself."""
    import main
    p = main.build_parser()
    args = p.parse_args(["validate", "--config", "x.json",
                         "--ratchet-timing", "same_bar"])
    assert args.ratchet_timing == "same_bar"
    cfg = {"defaults": {"ratchet_timing": "next_bar"},
           "variants": [{"name": "a", "ratchet_timing": "next_bar"}]}
    patched = {**cfg,
               "defaults": {**cfg["defaults"], "ratchet_timing": args.ratchet_timing},
               "variants": [{**v, "ratchet_timing": args.ratchet_timing}
                            for v in cfg["variants"]]}
    merged = main._merged_variants(patched, None)
    assert all(build_variant(v).ratchet_timing == RATCHET_SAME_BAR for v in merged)


def test_neither_timing_changes_a_trade_with_no_ratchet_rules():
    """The knob must not have side effects outside the case it is for."""
    outs = []
    for timing in (RATCHET_NEXT_BAR, RATCHET_SAME_BAR):
        s = series([4000.0, (3999.0, 4000.5, 3999.0, 4000.0),
                    (4000.0, 4011.0, 3998.0, 4000.0)] + [4020.0] * 6)
        v = build_variant(variant_dict(sl_rules=NO_RATCHET, ratchet_timing=timing))
        row = signal_row(signal(entry=4000.0, sl=3990.0, tps=(4010.0, 4020.0)),
                         at=s[0].ts)
        res = PortfolioSim(v, s).run([row])
        outs.append(([l.outcome for l in res.trades[0].legs],
                     res.trades[0].realized_pl))
    assert outs[0] == outs[1]
