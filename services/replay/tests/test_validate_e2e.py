"""The validation gate, end to end: sweep -> extract -> compare -> verdict.

`validate.report` and `store.sim_legs_for_validation` were each tested; their
COMPOSITION was not, and that is where the bug was — the call site dropped the
whole legs block while trying to drop one array inside it, so the gate printed a
verdict with no agreement rate behind it. These run the real chain on synthetic
bars so a wiring fault fails here instead of after a real run.
"""
from __future__ import annotations

import datetime as dt

from harness import runner as R
from harness import store, validate
from conftest import (NO_RATCHET, T0, series, signal, signal_row, variant_dict)


def _sweep():
    """A run where every signal fills and takes TP1 — deterministic, so the
    'live truth' can be written by hand and the agreement is exact."""
    spec = R.RunSpec(variants=[variant_dict(name="live", sl_rules=NO_RATCHET)])
    s = series([4020, 4020, (4006, 4006, 3999, 4005),
                (4005, 4032, 4004, 4031)] + [4031] * 8)
    rows = [signal_row(signal(), sid=i + 1, at=T0 + dt.timedelta(minutes=1))
            for i in range(3)]
    out = R.sweep(spec, s, rows)
    return out["results"]["live"]


def _truth_from(sim_legs, sim_trades, *, r_offset=0.0):
    """Fabricate broker truth that agrees with the simulation, optionally shifted
    in R so the bias term can be exercised."""
    live_legs = [dict(l) for l in sim_legs]
    live_trades = [{**t, "r": (t["r"] or 0) - r_offset} for t in sim_trades]
    return live_legs, live_trades


def test_the_chain_runs_and_the_gate_passes_on_a_perfect_reproduction():
    res = _sweep()
    sim_legs, sim_trades = store.sim_legs_for_validation(res)
    assert sim_legs and sim_trades, "the sweep must produce something to compare"
    live_legs, live_trades = _truth_from(sim_legs, sim_trades)
    rep = validate.report(sim_legs, live_legs, sim_trades, live_trades)
    assert rep["gate"]["passed"] is True


def test_the_report_keeps_the_numbers_the_gate_was_decided_from():
    """The regression. A pass/fail with no agreement rate and no error
    distribution is unactionable — on a failure it is the difference between
    diagnosing it and re-running blind."""
    res = _sweep()
    sim_legs, sim_trades = store.sim_legs_for_validation(res)
    live_legs, live_trades = _truth_from(sim_legs, sim_trades)
    rep = validate.report(sim_legs, live_legs, sim_trades, live_trades)
    assert "legs" in rep, "the legs summary must survive the trim"
    assert rep["legs"]["outcome"]["agreement_rate"] is not None
    assert "n_matched_legs" in rep["legs"]
    assert "n_only_sim" in rep["legs"] and "n_only_live" in rep["legs"]
    assert rep["legs"]["fill"]["n"] >= 0
    assert rep["r"]["median_abs"] is not None


def test_the_per_leg_detail_is_dropped_by_default_but_available_on_request():
    res = _sweep()
    sim_legs, sim_trades = store.sim_legs_for_validation(res)
    live_legs, live_trades = _truth_from(sim_legs, sim_trades)
    trimmed = validate.report(sim_legs, live_legs, sim_trades, live_trades)
    full = validate.report(sim_legs, live_legs, sim_trades, live_trades,
                           include_rows=True)
    assert "rows" not in trimmed["legs"]
    assert full["legs"]["rows"], "include_rows must still return the detail"


def test_a_rosier_simulation_fails_the_gate_through_the_real_chain():
    """The bias term, exercised on real simulator output rather than fixtures:
    a harness 0.3R better than live on every trade must not pass."""
    res = _sweep()
    sim_legs, sim_trades = store.sim_legs_for_validation(res)
    live_legs, live_trades = _truth_from(sim_legs, sim_trades, r_offset=0.3)
    rep = validate.report(sim_legs, live_legs, sim_trades, live_trades)
    assert rep["gate"]["passed"] is False
    assert rep["gate"]["systematic_bias"] == "optimistic"


def test_trades_live_took_that_the_sim_did_not_produce_are_counted():
    """A harness that simply fails to produce the trades live took would
    otherwise score a clean agreement rate on the handful it did produce."""
    res = _sweep()
    sim_legs, sim_trades = store.sim_legs_for_validation(res)
    live_legs, live_trades = _truth_from(sim_legs, sim_trades)
    live_legs.append({"signal_id": 999, "account_id": 1, "tp_index": 1,
                      "fill_price": 4000.0, "outcome": "tp_hit"})
    rep = validate.report(sim_legs, live_legs, sim_trades, live_trades)
    assert rep["legs"]["n_only_live"] == 1


def test_the_extract_covers_every_leg_of_every_simulated_trade():
    res = _sweep()
    sim_legs, sim_trades = store.sim_legs_for_validation(res)
    assert len(sim_trades) == len(res.trades)
    assert len(sim_legs) == sum(len(t.legs) for t in res.trades)
