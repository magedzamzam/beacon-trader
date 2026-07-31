"""The §5 validation gate.

The gate is the only reason to believe anything this harness says, so its
failure modes are tested harder than its success mode — particularly the
directional bias test, which is what stops a systematically-rosy simulator from
passing on scatter alone.
"""
from __future__ import annotations

from harness import validate


def _legs(n, *, outcome="tp_hit", live_outcome=None, fill=4000.0,
          live_fill=4000.0, direction="BUY"):
    sim = [{"signal_id": i, "account_id": 1, "tp_index": 1, "direction": direction,
            "fill_price": fill, "outcome": outcome} for i in range(n)]
    live = [{"signal_id": i, "account_id": 1, "tp_index": 1, "direction": direction,
             "fill_price": live_fill, "outcome": live_outcome or outcome}
            for i in range(n)]
    return sim, live


def _trades(deltas):
    sim = [{"signal_id": i, "account_id": 1, "r": 1.0 + d}
           for i, d in enumerate(deltas)]
    live = [{"signal_id": i, "account_id": 1, "r": 1.0}
            for i in range(len(deltas))]
    return sim, live


def test_a_perfect_reproduction_passes():
    sim, live = _legs(20)
    st, lt = _trades([0.0] * 20)
    rep = validate.report(sim, live, st, lt)
    assert rep["gate"]["passed"] is True
    assert rep["legs"]["outcome"]["agreement_rate"] == 1.0


def test_low_outcome_agreement_fails_the_gate():
    sim, live = _legs(20)
    for r in sim[:5]:
        r["outcome"] = "sl_hit"
    rep = validate.report(sim, live, *_trades([0.0] * 20))
    assert rep["legs"]["outcome"]["agreement_rate"] == 0.75
    assert rep["gate"]["passed"] is False


def test_systematic_optimism_is_a_blocking_defect_even_with_tight_scatter():
    """Scatter is noise; a consistent lean is a defect. A harness 0.2R rosier on
    EVERY trade would otherwise pass a median-error test comfortably."""
    sim, live = _legs(20)
    rep = validate.report(sim, live, *_trades([0.2] * 20))
    assert rep["gate"]["systematic_bias"] == "optimistic"
    assert rep["gate"]["passed"] is False


def test_systematic_pessimism_is_also_flagged():
    sim, live = _legs(20)
    rep = validate.report(sim, live, *_trades([-0.2] * 20))
    assert rep["gate"]["systematic_bias"] == "pessimistic"


def test_symmetric_scatter_without_a_lean_still_passes():
    sim, live = _legs(20)
    rep = validate.report(sim, live, *_trades([0.2, -0.2] * 10))
    assert rep["gate"]["systematic_bias"] is None
    assert rep["gate"]["passed"] is True


def test_wide_scatter_fails_on_the_median_even_without_a_lean():
    sim, live = _legs(20)
    rep = validate.report(sim, live, *_trades([0.6, -0.6] * 10))
    assert rep["gate"]["passed"] is False
    assert any("median" in f for f in rep["gate"]["failures"])


def test_a_zero_fill_price_is_unknown_and_is_excluded_not_scored():
    """A stored `fill_price = 0` means UNKNOWN (#159). Treating it as a number
    would report a 4000-point fill error and drown the real distribution."""
    sim, live = _legs(5)
    live[0]["fill_price"] = 0
    out = validate.compare(sim, live)
    assert out["fill"]["n"] == 4
    assert out["outcome"]["n"] == 4          # no live fill -> no outcome to compare


def test_unmatched_rows_are_counted_on_both_sides():
    """A harness that simply fails to produce the trades live took would
    otherwise score 100% on the handful it did produce."""
    sim, live = _legs(5)
    sim.append({"signal_id": 99, "account_id": 1, "tp_index": 1,
                "fill_price": 4000.0, "outcome": "tp_hit"})
    live.append({"signal_id": 77, "account_id": 1, "tp_index": 1,
                 "fill_price": 4000.0, "outcome": "tp_hit"})
    out = validate.compare(sim, live)
    assert out["n_matched_legs"] == 5
    assert out["n_only_sim"] == 1 and out["n_only_live"] == 1


def test_breakeven_and_sl_hit_agree_on_the_mechanism_but_the_relabel_is_counted():
    sim, live = _legs(10, outcome="breakeven", live_outcome="sl_hit")
    out = validate.compare(sim, live)
    assert out["outcome"]["agreement_rate"] == 1.0
    assert out["outcome"]["n_stop_family_relabels"] == 10


def test_a_tp_vs_sl_disagreement_never_counts_as_agreement():
    sim, live = _legs(10, outcome="tp_hit", live_outcome="sl_hit")
    out = validate.compare(sim, live)
    assert out["outcome"]["agreement_rate"] == 0.0


def test_no_comparable_data_fails_rather_than_passing_vacuously():
    rep = validate.report([], [], [], [])
    assert rep["gate"]["passed"] is False
    assert rep["gate"]["failures"]


def test_a_pooled_fill_delta_hides_a_systematic_entry_advantage():
    """The defect this metric was split to fix. A harness that fills 0.5 better
    on every trade averages to ZERO in the raw (sim - live) figure, because a
    BUY filling lower and a SELL filling higher are opposite signs and cancel."""
    buy_s, buy_l = _legs(10, fill=3999.5, live_fill=4000.0, direction="BUY")
    sell_s, sell_l = _legs(10, fill=4000.5, live_fill=4000.0, direction="SELL")
    for i, row in enumerate(sell_s):
        row["signal_id"] = sell_l[i]["signal_id"] = 100 + i
    out = validate.compare(buy_s + sell_s, buy_l + sell_l)
    assert out["fill"]["mean"] == 0.0                 # cancels — reads as clean
    assert out["fill_adverse"]["mean"] == -0.5        # and is caught here


def test_positive_adverse_delta_means_the_simulation_got_the_worse_fill():
    buy_s, buy_l = _legs(5, fill=4000.5, live_fill=4000.0, direction="BUY")
    assert validate.compare(buy_s, buy_l)["fill_adverse"]["mean"] == 0.5
    sell_s, sell_l = _legs(5, fill=3999.5, live_fill=4000.0, direction="SELL")
    assert validate.compare(sell_s, sell_l)["fill_adverse"]["mean"] == 0.5


def test_an_unknown_direction_is_excluded_rather_than_signed_by_guess():
    sim, live = _legs(5, fill=4000.5, live_fill=4000.0, direction=None)
    out = validate.compare(sim, live)
    assert out["fill"]["n"] == 5          # the raw difference is still measurable
    assert out["fill_adverse"]["n"] == 0  # the signed one is not invented


def test_the_unmodelled_execution_failures_are_named_in_the_report():
    """Confirm-404s and orphaned STOPs have no candle signature, so the harness
    is structurally optimistic by however often they happened. That has to be
    stated next to the number, not remembered."""
    rep = validate.report(*_legs(5), *_trades([0.0] * 5))
    joined = " ".join(rep["known_execution_reality"])
    assert "#150" in joined and "#161" in joined
    assert "structurally optimistic" in rep["note"]
