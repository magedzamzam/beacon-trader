"""Portfolio state: the caps, the breaker, and counterfactual coverage.

Section 5 is blunt about this — "model the risk caps and the breaker explicitly,
or the harness will systematically overstate every variant". These tests are the
proof that it does, and that a signal a cap refused is a ROW with a reason, not
an absence.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from harness import fills as F
from harness.portfolio import PortfolioSim
from harness.variants import build_variant
from conftest import (NO_RATCHET, T0, series, signal, signal_row, variant_dict)


def _series_for(n_flat=6):
    """Price rests above a 4000 BUY entry, dips to fill, then rallies to TP1."""
    return series([4020, 4020, (4006, 4006, 3999, 4005),
                   (4005, 4012, 4004, 4011)] + [4011] * n_flat)


def _rows(n, *, at_minute=1, source_id=7):
    return [signal_row(signal(), sid=i + 1, source_id=source_id,
                       at=T0 + dt.timedelta(minutes=at_minute))
            for i in range(n)]


def test_a_taken_signal_produces_a_trade_and_a_count():
    v = build_variant(variant_dict(sl_rules=NO_RATCHET))
    res = PortfolioSim(v, _series_for()).run(_rows(1))
    assert res.counts["taken"] == 1
    assert len(res.trades) == 1
    assert res.trades[0].ever_filled is True


def test_the_open_risk_cap_blocks_the_second_concurrent_signal():
    """The 2026-07-27 mechanism: the cap silently decides WHICH signals an
    account takes. A harness that ignored it would score both as taken."""
    v = build_variant(variant_dict(
        sl_rules=NO_RATCHET,
        risk={"default": {"basis": "fixed_cash", "value": 100}},
        risk_limits={"enabled": True, "daily_loss_limit": 0,
                     "max_open_risk_per_symbol": 150,
                     "max_open_risk_per_account": 0, "max_signal_risk_pct": 0}))
    res = PortfolioSim(v, _series_for()).run(_rows(2))
    assert res.counts["taken"] == 1
    assert res.counts["risk_limit_block"] == 1
    assert res.n_signals_blocked_by_caps == 1
    blocked = next(r for r in res.not_taken if r["reason"] == "risk_limit_block")
    assert "over cap" in blocked["detail"]


def test_a_blocked_signal_is_a_row_with_a_reason_not_an_absence():
    v = build_variant(variant_dict(
        sl_rules=NO_RATCHET,
        risk_limits={"enabled": True, "trading_halted": True}))
    res = PortfolioSim(v, _series_for()).run(_rows(3))
    assert res.counts["taken"] == 0
    assert len(res.not_taken) == 3
    assert {r["signal_id"] for r in res.not_taken} == {1, 2, 3}
    assert all(r["reason"] == "risk_limit_block" for r in res.not_taken)


def test_the_daily_loss_breaker_pauses_new_entries_after_a_loss():
    """A hard daily-loss halt is a portfolio fact, so it can only be modelled by
    replaying signals in order against accumulated realized P&L."""
    v = build_variant(variant_dict(
        sl_rules=NO_RATCHET,
        risk={"default": {"basis": "fixed_cash", "value": 100}},
        risk_limits={"enabled": True, "daily_loss_limit": 50,
                     "per_signal_max_pct_of_daily": 0,
                     "max_open_risk_per_symbol": 0,
                     "max_open_risk_per_account": 0, "max_signal_risk_pct": 0}))
    # first signal fills and stops out for -100; the second arrives later, into
    # a price that is still a valid entry — so the ONLY thing that can decline
    # it is the breaker.
    s = series([4020, (4006, 4006, 3999, 4005), (4005, 4005, 3985, 4005)]
               + [4005] * 10)
    rows = [signal_row(signal(), sid=1, at=T0 + dt.timedelta(minutes=1)),
            signal_row(signal(), sid=2, at=T0 + dt.timedelta(minutes=6))]
    res = PortfolioSim(v, s).run(rows)
    assert res.counts["taken"] == 1
    assert res.counts["risk_limit_block"] == 1
    blocked = next(r for r in res.not_taken if r["reason"] == "risk_limit_block")
    assert "daily loss limit" in blocked["detail"]


def test_a_filtration_skip_is_recorded_so_the_rejected_set_is_scoreable():
    """"For signals Arm B skipped, what did they do?" is only answerable if the
    skip is a row."""
    v = build_variant(variant_dict(strategies=[{
        "account_id": None, "source_id": None,
        "entry_policy": {"entry_style": "limit", "ttl_minutes": 60},
        "entry_filters": {"rules": [
            {"enabled": True, "name": "always-skip", "mode": "live",
             "when": {"type": "always"}, "action": "skip"}]},
        "exit_policy": {"sl_rules": NO_RATCHET},
    }]))
    res = PortfolioSim(v, _series_for()).run(_rows(2))
    assert res.counts["taken"] == 0
    assert res.counts["filtration_skip"] == 2


def test_a_filtration_scale_desizes_the_plan():
    def _risk(rules):
        v = build_variant(variant_dict(
            risk={"default": {"basis": "fixed_cash", "value": 100}},
            strategies=[{"account_id": None, "source_id": None,
                         "entry_policy": {"entry_style": "limit"},
                         "entry_filters": {"rules": rules},
                         "exit_policy": {"sl_rules": NO_RATCHET}}]))
        res = PortfolioSim(v, _series_for()).run(_rows(1))
        return res.trades[0].planned_risk

    full = _risk([])
    half = _risk([{"enabled": True, "name": "half", "mode": "live",
                   "when": {"type": "always"}, "action": "scale", "factor": 0.5}])
    assert half < full
    # Not exactly half: the scale lands on the RISK CONFIG (as it does live), so
    # the lot is re-rounded to `lot_step` afterwards. Asserting exact halving
    # would be asserting that lot rounding does not exist.
    assert abs(float(half) / float(full) - 0.5) < 0.01


def test_a_signal_outside_the_candle_window_is_excluded_not_silently_dropped():
    v = build_variant(variant_dict(sl_rules=NO_RATCHET))
    late = signal_row(signal(), sid=9, at=T0 + dt.timedelta(days=30))
    res = PortfolioSim(v, _series_for()).run([late])
    assert res.counts["no_candle_coverage"] == 1
    assert res.not_taken[0]["reason"] == "no_candle_coverage"


def test_every_signal_lands_in_exactly_one_bucket():
    """Taken + not-taken must account for the whole input. A signal that fell
    through a crack would make every rate in the report wrong by an unknown
    amount."""
    v = build_variant(variant_dict(
        sl_rules=NO_RATCHET,
        risk={"default": {"basis": "fixed_cash", "value": 100}},
        risk_limits={"enabled": True, "daily_loss_limit": 0,
                     "max_open_risk_per_symbol": 250,
                     "max_open_risk_per_account": 0, "max_signal_risk_pct": 0}))
    rows = _rows(5)
    res = PortfolioSim(v, _series_for()).run(rows)
    assert res.counts["taken"] + len(res.not_taken) == len(rows)


def test_open_risk_is_released_when_a_trade_finishes():
    """Otherwise the cap would ratchet shut over a run and every late signal
    would be blocked for a reason live never had."""
    v = build_variant(variant_dict(
        sl_rules=NO_RATCHET,
        risk={"default": {"basis": "fixed_cash", "value": 100}},
        risk_limits={"enabled": True, "daily_loss_limit": 0,
                     "max_open_risk_per_symbol": 150,
                     "max_open_risk_per_account": 0, "max_signal_risk_pct": 0}))
    s = series([4020, (4006, 4006, 3999, 4005), (4005, 4012, 4004, 4011),
                4011, 4011, (4011, 4011, 3999, 4005), 4005, 4005])
    one_tp = signal(tps=(4010.0,))               # a ladder that fully resolves
    rows = [signal_row(one_tp, sid=1, at=T0 + dt.timedelta(minutes=1)),
            signal_row(one_tp, sid=2, at=T0 + dt.timedelta(minutes=5))]
    res = PortfolioSim(v, s).run(rows)
    assert res.counts["taken"] == 2, "the first trade had closed by minute 5"


def test_a_never_filled_trade_is_reported_and_carries_no_pnl():
    v = build_variant(variant_dict(sl_rules=NO_RATCHET,
                                   entry_policy={"entry_style": "limit",
                                                 "ttl_minutes": 3}))
    s = series([4020] * 12)
    res = PortfolioSim(v, s).run(_rows(1))
    assert res.counts["never_filled"] == 1
    assert res.trades[0].realized_pl == Decimal("0")
    assert all(l.status == F.EXPIRED for l in res.trades[0].legs)
