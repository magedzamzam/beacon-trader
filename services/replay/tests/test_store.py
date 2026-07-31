"""The persistence shape (the pure half of `store.py`).

`replay_results` is what every downstream query reads, so the flattening is
tested directly: a taken trade must carry its R, and a DECLINED signal must be a
row with a reason. If declines were dropped here, "how many did this variant's
caps block?" would silently become unanswerable at the exact layer an analyst
looks.
"""
from __future__ import annotations

import datetime as dt

from harness import store
from harness.portfolio import PortfolioSim
from harness.variants import build_variant
from conftest import (NO_RATCHET, T0, series, signal, signal_row, variant_dict)


def _res(**vkw):
    v = build_variant(variant_dict(sl_rules=NO_RATCHET, **vkw))
    s = series([4020, 4020, (4006, 4006, 3999, 4005),
                (4005, 4032, 4004, 4031)] + [4031] * 6)
    rows = [signal_row(signal(), sid=1, at=T0 + dt.timedelta(minutes=1))]
    return v, PortfolioSim(v, s).run(rows)


def test_a_taken_trade_becomes_a_row_with_its_r_multiple():
    _v, res = _res()
    rows = store.result_rows(7, "v", res)
    assert len(rows) == 1
    r = rows[0]
    assert r["run_id"] == 7 and r["variant"] == "v" and r["taken"] is True
    assert r["signal_id"] == 1 and r["account_id"] == 1
    assert r["ever_filled"] is True
    assert r["r_multiple"] is not None
    assert r["legs"] and r["legs"][0]["outcome"]


def test_a_declined_signal_is_a_row_not_an_absence():
    _v, res = _res(risk_limits={"enabled": True, "trading_halted": True})
    rows = store.result_rows(7, "v", res)
    assert len(rows) == 1
    assert rows[0]["taken"] is False
    assert rows[0]["not_taken_reason"] == "risk_limit_block"


def test_the_holdout_split_is_stamped_on_each_row():
    _v, res = _res()
    before = store.result_rows(7, "v", res,
                               holdout_from=T0 + dt.timedelta(days=1))
    after = store.result_rows(7, "v", res,
                              holdout_from=T0 - dt.timedelta(days=1))
    assert before[0]["in_sample"] is True
    assert after[0]["in_sample"] is False


def test_leg_rows_carry_outcome_labels_but_no_money():
    """Trade-level P&L is trustworthy; leg-level is not (CLAUDE.md §2.5). A
    leg-level money column here would invite exactly the analysis the repo has
    already ruled out."""
    _v, res = _res()
    leg = store.result_rows(7, "v", res)[0]["legs"][0]
    assert "outcome" in leg and "tp_index" in leg
    assert "realized_pl" not in leg


def test_the_validation_extract_is_keyed_the_way_the_gate_compares():
    _v, res = _res()
    legs, trades = store.sim_legs_for_validation(res)
    assert legs and trades
    for row in legs:
        assert {"signal_id", "account_id", "tp_index"} <= set(row)
    for row in trades:
        assert {"signal_id", "account_id", "r"} <= set(row)


def test_a_run_with_no_trades_produces_no_result_rows():
    _v, res = _res()
    res.trades, res.not_taken = [], []
    assert store.result_rows(7, "v", res) == []
