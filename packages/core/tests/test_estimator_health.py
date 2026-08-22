"""The regime label went degenerate for the third time, and the guard (#255).

`classify_regime` ORed `adx >= 25` with `hurst > 0.55`. #168 repaired the Hurst
ESTIMATOR (R/S on log returns, not on the price level) and the label still went
117/117 `trending` in the week to 2026-08-22, because the repaired H lands in
[0.5063, 1.0591] on this book — above 0.55 on 644 of 671 captured signals. An OR
is only as discriminating as its widest term, so a term that is true 96% of the
time swallowed the one that works: ADX alone splits the same window 338/218 and
the frozen week 92/25.

Two things are pinned here. The label must be capable of taking both values on
inputs the book actually produces, and — because the next constant can saturate
just as quietly — a degenerate OUTPUT must raise by itself rather than being
discovered by an analyst three weeks running.
"""
from pathlib import Path

from beacon_core.analysis import estimators as E
from beacon_core.notifications import config as NC
from beacon_core.notifications import templates as NT

REPO_ROOT = Path(__file__).resolve().parents[3]


# --- the label can take both values -------------------------------------------
def test_the_adx_side_is_provably_live():
    """The acceptance criterion, stated as the issue states it: a synthetic
    low-ADX, low-Hurst input must read `ranging`. If the ADX/ATR side of the old
    OR were still dead — #111's key mismatch — nothing could reach this."""
    assert E.classify_regime(10.0, 0.2, 0.1, 0.4) == "ranging"


def test_hurst_cannot_outvote_adx_at_any_value_it_actually_takes():
    """The empirical range of the repaired estimator on this book, end to end.
    Every one of these used to force `trending`; none of them may now."""
    for h in (0.5063, 0.5531, 0.5579, 0.6241, 0.6708, 1.0591):
        assert E.classify_regime(10.0, 0.2, 0.1, h) == "ranging"


def test_a_window_of_real_adx_values_produces_both_labels():
    """`regime` takes both values over a 100-signal window. The ADX values are
    the observed quartiles of the last 60 days (min 10.01, median 27.12, max
    63.24), so this is the book's own distribution, not a convenient one."""
    adxs = [10.01, 18.4, 22.7, 24.9, 27.12, 31.0, 44.5, 63.24] * 13    # 104
    labels = {E.classify_regime(a, 0.25, 0.1, 0.62) for a in adxs}
    assert labels == {"trending", "ranging"}


def test_a_missing_adx_reads_unknown_rather_than_guessing():
    """115 of the last 671 captured signals have no ADX in the regime block.
    Labelling those from Hurst would relabel all 115 `trending` — the same
    degeneracy in a smaller subset — and labelling them `ranging` is a guess that
    a conditioned analysis then treats as data."""
    assert E.classify_regime(None, None, 0.1, 0.99) == "unknown"
    assert E.classify_regime(None, None, None, None) == "unknown"


def test_a_volatility_spike_still_dominates():
    # The one priority the repair must not disturb.
    assert E.classify_regime(40.0, 0.2, 1.5, 0.7) == "high_vol"


# --- the guard ----------------------------------------------------------------
def test_the_guard_fires_when_every_row_shares_a_label():
    """The acceptance criterion, on the actual frozen week: 117 trending, nothing
    else."""
    v = E.degenerate_label({"trending": 117})
    assert v["degenerate"] and v["n"] == 117 and v["top_label"] == "trending"
    assert "nothing can be conditioned on it" in v["reason"]


def test_the_guard_stays_quiet_on_a_lopsided_but_varying_label():
    """2026-08-17 was 123/9 — degraded, not degenerate. There is no defensible
    constant for "too skewed", so the share is REPORTED and the alarm is not
    fired: an alarm whose boundary is arguable gets argued with instead of acted
    on."""
    v = E.degenerate_label({"trending": 123, "ranging": 9})
    assert not v["degenerate"]
    assert v["top_share"] == round(123 / 132, 4)


def test_one_label_on_a_thin_window_is_a_quiet_week_not_a_defect():
    v = E.degenerate_label({"trending": 40})
    assert not v["degenerate"] and "below the 50 needed" in v["reason"]
    assert E.degenerate_label({"trending": 40}, min_n=30)["degenerate"]


def test_the_floor_is_inclusive():
    # Exactly min_n observations, all one label, IS degenerate.
    assert E.degenerate_label({"trending": E.DEGENERATE_MIN_N})["degenerate"]


def test_empty_and_zero_counts_are_not_a_degenerate_label():
    """No captures is a broken pipeline, not a flat estimator, and firing this
    alarm for it would point the operator at the wrong thing."""
    assert not E.degenerate_label({})["degenerate"]
    assert not E.degenerate_label({"trending": 0, "ranging": 0})["degenerate"]


def test_the_guard_is_estimator_agnostic():
    """It takes counts, so any categorical output the capture persists can be
    watched without this module knowing what the labels mean."""
    v = E.degenerate_label({"bull": 80})
    assert v["degenerate"] and v["top_label"] == "bull"


# --- the alarm is actually wired ----------------------------------------------
def test_estimator_degenerate_is_routable_and_really_emitted():
    """`daily_summary` is the cautionary tale: routed, given an emoji, and fired
    by nothing (#198). A guard nobody is told about is the state this issue is
    already complaining about."""
    assert "estimator_degenerate" in NC.EVENT_IDS
    assert NT.is_emitted("estimator_degenerate"), "must carry a field contract"
    monitor = (REPO_ROOT / "services/monitor/main.py").read_text(encoding="utf-8")
    assert '_notify("estimator_degenerate"' in monitor, "no service fires it"
    assert "await _check_degenerate_labels()" in monitor, "the check must run each tick"


def test_the_alarm_does_not_wake_the_operator_at_three_in_the_morning():
    """Measurement integrity, not money. An unmapped event defaults to `critical`
    on purpose, so this one has to say otherwise deliberately."""
    assert NC.event_severity("estimator_degenerate") == "summary"
    assert NC.event_severity("arm_dark") == "critical"      # money/safety, unchanged
