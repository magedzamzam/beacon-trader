"""Metrics + the overfitting guardrails.

The rollup itself is `beacon_core.analysis.report.geometry_ab_rollup` — not a
copy — so these tests assert the FEEDING is right (which trades count, how they
are split) and that the guardrails are enforced rather than documented.
"""
from __future__ import annotations

import datetime as dt

from harness import metrics
from harness.portfolio import PortfolioSim
from harness.variants import build_variant
from conftest import (NO_RATCHET, T0, series, signal, signal_row, variant_dict)

LIVE_KEYS = {"avg_R", "expectancy_R", "payoff_ratio", "profit_factor",
             "breakeven_leg_rate", "pct_winners_reach_tp3", "win_rate_ci",
             "net_nominal"}


def _run(n_signals=2, *, minutes=1, mids=None, **vkw):
    v = build_variant(variant_dict(sl_rules=NO_RATCHET, **vkw))
    s = series(mids or ([4020, 4020, (4006, 4006, 3999, 4005),
                         (4005, 4032, 4004, 4031)] + [4031] * 6))
    rows = [signal_row(signal(), sid=i + 1, at=T0 + dt.timedelta(minutes=minutes))
            for i in range(n_signals)]
    return v, s, PortfolioSim(v, s).run(rows)


def test_the_rollup_emits_the_live_metric_keys():
    """"Emit the SAME metric keys as live" is only true if it is the same
    function — so this asserts the shape a weekly report reads."""
    v, s, res = _run(1)
    rep = metrics.variant_report(res, variant=v, series=s)
    arm = rep["pooled"]["by_arm"][0]
    assert LIVE_KEYS.issubset(arm.keys())
    assert rep["pooled"]["n_closed"] == 1


def test_never_filled_trades_are_excluded_from_the_rollup_not_scored_as_zeros():
    """Counting an expired entry as a 0-P&L loss would drag every variant's win
    rate toward the share of orders that never filled."""
    v, s, res = _run(1, mids=[4020] * 12,
                     entry_policy={"entry_style": "limit", "ttl_minutes": 3})
    rep = metrics.variant_report(res, variant=v, series=s)
    assert rep["caveats"]["n_never_filled"] == 1
    assert rep["pooled"]["n_closed"] == 0


def test_the_verdict_is_withheld_below_the_live_significance_floor():
    v, s, res = _run(1)
    rep = metrics.variant_report(res, variant=v, series=s)
    assert rep["min_trades_for_verdict"] == 30
    assert rep["verdict_withheld"] is True


def test_without_a_holdout_the_headline_is_labelled_in_sample():
    v, s, res = _run(1)
    rep = metrics.variant_report(res, variant=v, series=s)
    assert rep["headline_basis"] == "in_sample"
    assert rep["held_out"] is None
    assert rep["guardrails"]["walk_forward"]["enabled"] is False


def test_with_a_holdout_the_headline_is_the_held_out_set():
    v, s, res = _run(1)
    rep = metrics.variant_report(res, variant=v, series=s,
                                 holdout_from=T0 - dt.timedelta(days=1))
    assert rep["headline_basis"] == "held_out"
    assert rep["held_out"] is not None
    assert rep["headline"]["n_closed"] == rep["held_out"]["n_closed"]


def test_the_split_puts_pre_holdout_trades_in_sample():
    v, s, res = _run(1)
    rep = metrics.variant_report(res, variant=v, series=s,
                                 holdout_from=T0 + dt.timedelta(days=1))
    assert rep["held_out"]["n_closed"] == 0
    assert rep["in_sample"]["n_closed"] == 1


def test_the_search_size_rides_on_every_report():
    v, s, res = _run(1)
    rep = metrics.variant_report(res, variant=v, series=s, n_variants_searched=20)
    g = rep["guardrails"]
    assert g["n_variants_searched"] == 20
    assert g["best_of_n_inflation_sigma"] > 2.0     # sqrt(2 ln 20) ~ 2.45


def test_best_of_n_inflation_is_none_for_a_single_variant():
    assert metrics.best_of_n_inflation(1) is None
    assert metrics.best_of_n_inflation(2) is not None


def test_per_source_results_are_reported_alongside_pooled():
    """The correct exit almost certainly differs by channel, so a pooled-only
    answer averages away the thing being measured (#182)."""
    v = build_variant(variant_dict(sl_rules=NO_RATCHET))
    s = series([4020, 4020, (4006, 4006, 3999, 4005),
                (4005, 4032, 4004, 4031)] + [4031] * 6)
    rows = [signal_row(signal(), sid=1, source_id=7,
                       at=T0 + dt.timedelta(minutes=1)),
            signal_row(signal(), sid=2, source_id=9,
                       at=T0 + dt.timedelta(minutes=1))]
    res = PortfolioSim(v, s).run(rows)
    rep = metrics.variant_report(res, variant=v, series=s,
                                 sources_by_id={7: "TFXC", 9: "Quartz"})
    assert set(rep["by_source"]) == {"7", "9"}
    assert rep["by_source"]["7"]["source"] == "TFXC"
    assert rep["by_source"]["9"]["verdict_withheld"] is True


def test_the_caveat_block_carries_the_unknowns_as_headline_numbers():
    v, s, res = _run(1)
    c = metrics.variant_report(res, variant=v, series=s)["caveats"]
    for key in ("n_never_filled", "n_blocked_by_risk_limits",
                "n_blocked_by_breaker", "n_horizon_capped",
                "n_same_bar_ambiguous_legs", "suspect_bars_excluded",
                "not_taken_breakdown"):
        assert key in c


def test_the_suspect_bar_count_reaches_the_report():
    from harness import bars as B
    from conftest import path_bars
    v, _s, res = _run(1)
    s = B.BarSeries(path_bars([4020] * 12), suspect_excluded=42)
    res.coverage = s.coverage()
    rep = metrics.variant_report(res, variant=v, series=s)
    assert rep["caveats"]["suspect_bars_excluded"] == 42


def test_regime_composition_refuses_to_classify_a_short_series():
    from harness import bars as B
    from conftest import path_bars
    out = metrics.regime_composition(B.BarSeries(path_bars([4020] * 10)))
    assert out["trending_share"] is None
    assert "too short" in out["note"]


def test_regime_composition_classifies_a_long_enough_series():
    from harness import bars as B
    from conftest import path_bars
    mids = []
    for i in range(400):                       # a clean uptrend -> trending
        p = 4000 + i * 2
        mids.append((p, p + 3, p - 3, p + 1))
    s = B.BarSeries(path_bars(mids, step_minutes=240))
    out = metrics.regime_composition(s)
    assert out["n_scored"] > 0
    assert out["trending_share"] is not None
    assert 0.0 <= out["trending_share"] <= 1.0


def test_the_promotion_caveat_is_not_optional():
    v, s, res = _run(1)
    rep = metrics.variant_report(res, variant=v, series=s)
    assert "HYPOTHESIS-GENERATING" in rep["guardrails"]["promotion"]
