"""Sub-ATR stop geometry, and the unit trap it is built around (#189).

Tight stops cost -18.19R on the control arm in one frozen week — more than the
entire winners' book returned (+16.43R). This suite pins the label, the
counterfactual, and above all the conversion, because #189 names the specific
mistake: `atr_pct` is a PERCENT OF PRICE, and comparing a stop distance against
it unconverted compares dollars to percent and produces a confidently wrong
verdict.

Pure — stdlib only, no DB.
"""
import pytest

from beacon_core.analysis.stop_geometry import (ATR_TIMEFRAME, DEFAULT_FLOOR,
                                                WIDENED_REACHED_TP1,
                                                WIDENED_STOPPED,
                                                WIDENED_UNRESOLVED,
                                                atr_abs_from_pct, rollup,
                                                shadow_label, stop_geometry,
                                                widen_and_resize)

# Gold near 4000 with a 0.35% ATR — 14 points, the realistic case.
PRICE, ATR_PCT = 4000.0, 0.35
ATR_ABS = 14.0


def test_atr_pct_is_a_percent_of_price_not_a_distance():
    """THE TRAP, stated as arithmetic."""
    assert atr_abs_from_pct(ATR_PCT, PRICE) == pytest.approx(ATR_ABS)


def test_a_stop_is_measured_against_the_converted_atr_not_the_raw_percent():
    """This test fails if anyone compares `|entry - sl|` to `atr_pct` directly.

    A 10-point stop on gold is 0.71 ATR — TIGHT, and squarely the population
    that cost -18.19R. Unconverted, 10 vs 0.35 would read as ~28x ATR, i.e. an
    extravagantly wide stop, and the finding would invert."""
    geo = stop_geometry(entry=4000.0, sl=3990.0, atr_pct=ATR_PCT, price=PRICE)
    assert geo["stop_atr_ratio"] == pytest.approx(10.0 / 14.0, rel=1e-3)
    assert geo["stop_below_atr_floor"] is True

    naive_ratio = 10.0 / ATR_PCT                       # the bug, made explicit
    assert naive_ratio > 28
    assert geo["stop_atr_ratio"] < 1.0
    assert naive_ratio != pytest.approx(geo["stop_atr_ratio"])


def test_a_wide_stop_is_not_flagged():
    geo = stop_geometry(entry=4000.0, sl=3970.0, atr_pct=ATR_PCT, price=PRICE)
    assert geo["stop_atr_ratio"] == pytest.approx(30.0 / 14.0, rel=1e-3)
    assert geo["stop_below_atr_floor"] is False


def test_the_atr_timeframe_is_recorded_because_a_ratio_needs_one():
    """1x ATR on 5m and 1x ATR on 4h are different distances."""
    assert stop_geometry(entry=4000.0, sl=3990.0, atr_pct=ATR_PCT,
                         price=PRICE)["atr_timeframe"] == ATR_TIMEFRAME


def test_an_unmeasurable_atr_is_none_not_a_default():
    """A missing ATR is not a wide stop, and must never be scored as one."""
    for kw in ({"atr_pct": None, "price": PRICE}, {"atr_pct": ATR_PCT, "price": None},
               {"atr_pct": 0, "price": PRICE}, {"atr_pct": "x", "price": PRICE}):
        assert stop_geometry(entry=4000.0, sl=3990.0, **kw) is None
    assert stop_geometry(entry=4000.0, sl=4000.0, atr_abs=ATR_ABS) is None


# --- the counterfactual -------------------------------------------------------
def _cf(mae_r, mfe_r, tp1=4010.0, floor=DEFAULT_FLOOR):
    return widen_and_resize(entry=4000.0, sl=3990.0, mae_r=mae_r, mfe_r=mfe_r,
                            tp1=tp1, atr_abs=ATR_ABS, floor=floor)


def test_a_stop_out_that_the_wider_stop_also_takes_is_still_minus_one_r():
    """Risk is held CONSTANT, so a stop-out costs exactly 1R either way. Widening
    is not free — that is the whole point of resizing rather than just widening."""
    cf = _cf(mae_r=-2.0, mfe_r=0.2)          # 20 points adverse vs a 14-point stop
    assert cf["counterfactual_outcome"] == WIDENED_STOPPED
    assert cf["counterfactual_r"] == -1.0


def test_a_stop_out_the_wider_stop_survives_and_that_reaches_tp1():
    """The mechanism: price breathed 12 points against a 10-point stop, then ran
    to target. A 14-point stop holds through the noise."""
    cf = _cf(mae_r=-1.2, mfe_r=1.5)          # 12 pts adverse, 15 pts favourable
    assert cf["counterfactual_outcome"] == WIDENED_REACHED_TP1
    # TP1 is 10 points away against a 14-point stop -> LESS than 1R.
    assert cf["counterfactual_r"] == pytest.approx(10.0 / 14.0, rel=1e-3)


def test_widening_makes_the_same_target_worth_less_in_r():
    """The honest cost, included rather than netted out."""
    cf = _cf(mae_r=-1.2, mfe_r=1.5)
    assert cf["counterfactual_r"] < 1.0
    assert cf["widen_factor"] == pytest.approx(1.4, rel=1e-3)


def test_a_survivor_that_never_reached_tp1_books_nothing():
    cf = _cf(mae_r=-0.5, mfe_r=0.3)
    assert cf["counterfactual_outcome"] == WIDENED_UNRESOLVED
    assert cf["counterfactual_r"] == 0.0


def test_reject_is_reported_separately_because_it_is_a_volume_cut():
    """Rejecting books nothing, which flatters a losing week for reasons that
    have nothing to do with stop placement. It must not be conflated with the
    risk-constant variant."""
    cf = _cf(mae_r=-2.0, mfe_r=0.2)
    assert cf["reject_r"] == 0.0 and cf["counterfactual_r"] == -1.0


def test_the_counterfactual_needs_an_excursion():
    assert widen_and_resize(entry=4000.0, sl=3990.0, mae_r=None, mfe_r=1.0,
                            tp1=4010.0, atr_abs=ATR_ABS) is None


# --- the shadow record and its rollup -----------------------------------------
def test_the_shadow_label_is_marked_as_shadow():
    """`sidecar.py`'s measure-before-gate invariant: this never gates anything."""
    lab = shadow_label(entry=4000.0, sl=3990.0, atr_abs=ATR_ABS,
                       mae_r=-1.2, mfe_r=1.5, tp1=4010.0)
    assert lab["shadow"] is True
    assert lab["counterfactual"]["counterfactual_outcome"] == WIDENED_REACHED_TP1


def test_the_rollup_separates_the_bucket_a_gate_would_act_on():
    tight = shadow_label(entry=4000.0, sl=3990.0, atr_abs=ATR_ABS,
                         mae_r=-1.2, mfe_r=1.5, tp1=4010.0)
    tight["actual_r"] = -1.0                       # it stopped, live
    wide = shadow_label(entry=4000.0, sl=3970.0, atr_abs=ATR_ABS,
                        mae_r=-0.3, mfe_r=1.0, tp1=4010.0)
    out = rollup([tight, wide])
    assert out["n"] == 2 and out["n_below_floor"] == 1
    assert out["below_floor"]["n_would_have_reached_tp1"] == 1
    assert out["below_floor"]["actual_total_r"] == -1.0
    assert out["below_floor"]["widen_total_r"] > 0
    assert out["n_to_confirm"] == 30


def test_the_rollup_states_that_nothing_gates_on_it():
    out = rollup([])
    assert "SHADOW" in out["note"] and "N>=30" in out["note"]
