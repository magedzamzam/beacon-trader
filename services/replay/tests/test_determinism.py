"""Reproducibility: same inputs -> same results, byte for byte.

"Re-running a run_id is byte-identical" is an acceptance criterion, and it is
the property that makes a replay auditable rather than anecdotal. The two ways
it usually breaks are a wall clock and an unsorted iteration, so both are
asserted here directly.
"""
from __future__ import annotations

import datetime as dt
import json

from harness import runner as R
from harness.portfolio import PortfolioSim
from harness.variants import build_variant
from conftest import (NO_RATCHET, T0, series, signal, signal_row, variant_dict)


def _mids():
    return [4020, 4020, (4006, 4006, 3999, 4005),
            (4005, 4032, 4004, 4031)] + [4031] * 8


def _rows(n=4):
    return [signal_row(signal(), sid=i + 1, source_id=7 + (i % 2),
                       at=T0 + dt.timedelta(minutes=1 + i))
            for i in range(n)]


def _fingerprint(res) -> str:
    return json.dumps([{
        "signal": t.signal_id, "account": t.account_id,
        "pl": str(t.realized_pl), "risk": str(t.planned_risk),
        "legs": [(l.tp_index, l.status, l.outcome, l.fill_price, l.close_price,
                  l.sl, l.sl_moved) for l in t.legs],
    } for t in res.trades] + [dict(sorted(r.items(), key=lambda kv: kv[0]))
                              for r in res.not_taken],
        sort_keys=True, default=str)


def test_the_same_run_twice_produces_identical_results():
    v = build_variant(variant_dict(sl_rules=NO_RATCHET))
    s = series(_mids())
    a = PortfolioSim(v, s).run(_rows())
    b = PortfolioSim(build_variant(variant_dict(sl_rules=NO_RATCHET)), s).run(_rows())
    assert _fingerprint(a) == _fingerprint(b)


def test_signal_input_order_does_not_change_the_outcome():
    """Arrivals are sorted by (time, id) before anything else happens, so a
    differently-ordered signal list — a different SQL plan, say — cannot change
    which signals a cap admitted."""
    v = build_variant(variant_dict(
        sl_rules=NO_RATCHET,
        risk={"default": {"basis": "fixed_cash", "value": 100}},
        risk_limits={"enabled": True, "daily_loss_limit": 0,
                     "max_open_risk_per_symbol": 250,
                     "max_open_risk_per_account": 0, "max_signal_risk_pct": 0}))
    s = series(_mids())
    fwd = PortfolioSim(v, s).run(_rows())
    rev = PortfolioSim(build_variant(variant_dict(
        sl_rules=NO_RATCHET,
        risk={"default": {"basis": "fixed_cash", "value": 100}},
        risk_limits={"enabled": True, "daily_loss_limit": 0,
                     "max_open_risk_per_symbol": 250,
                     "max_open_risk_per_account": 0, "max_signal_risk_pct": 0})),
        s).run(list(reversed(_rows())))
    assert _fingerprint(fwd) == _fingerprint(rev)


def test_a_sweep_is_reported_in_sorted_variant_order():
    """Assembled by NAME, never by completion order — otherwise a parallel run
    would differ from a sequential one in the one way that matters."""
    spec = R.RunSpec(variants=[variant_dict(name="zeta", sl_rules=NO_RATCHET),
                               variant_dict(name="alpha", sl_rules=NO_RATCHET)])
    out = R.sweep(spec, series(_mids()), _rows())
    assert list(out["variants"]) == ["alpha", "zeta"]


def test_the_run_digest_is_stable_and_change_sensitive():
    a = R.RunSpec(label="x", variants=[variant_dict(name="v")])
    b = R.RunSpec(label="x", variants=[variant_dict(name="v")])
    c = R.RunSpec(label="x", variants=[variant_dict(name="v", horizon_bars=99)])
    assert a.digest() == b.digest()
    assert a.digest() != c.digest()


def test_the_run_header_carries_the_reproducibility_fields():
    spec = R.RunSpec(label="x", variants=[variant_dict(name="v",
                                                       sl_rules=NO_RATCHET)])
    out = R.sweep(spec, series(_mids()), _rows())
    head = out["run"]
    assert head["config_digest"] and head["code_version"]
    assert head["coverage"]["n_bars"] > 0
    assert "HYPOTHESIS-GENERATING" in head["promotion"]


def test_the_ranking_shows_n_and_the_withheld_verdict_beside_the_metric():
    """A table that lets the eye read the ordering without the N is the failure
    mode the guardrails exist to prevent."""
    spec = R.RunSpec(variants=[variant_dict(name="a", sl_rules=NO_RATCHET),
                               variant_dict(name="b", sl_rules=NO_RATCHET)])
    out = R.sweep(spec, series(_mids()), _rows())
    ranking = R.compare_variants(out["variants"])
    assert len(ranking) == 2
    for row in ranking:
        assert "n_closed" in row and "verdict_withheld" in row
        assert row["n_variants_searched"] == 2


def test_an_empty_sweep_is_not_an_error():
    out = R.sweep(R.RunSpec(), series(_mids()), _rows())
    assert out["variants"] == {}
