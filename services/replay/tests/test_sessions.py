"""Session windows (#81): the risk multiplier and the `session_in` rule.

Live, the London/NY overlap de-sizes an entry through
`th_service.session_risk_multiplier`, and `session_in` filter rules match
against the active session labels. Both are computed from the SAME pure
`trading_hours.sessions` functions here, so a replayed overlap de-sizes by
exactly the factor live would apply — and a variant that does not configure
sessions says so on its result rather than leaving it to be inferred from a
divergence.
"""
from __future__ import annotations

import datetime as dt

from harness import metrics
from harness.portfolio import PortfolioSim
from harness.variants import build_variant
from conftest import NO_RATCHET, T0, series, signal, signal_row, variant_dict

# One window, UTC, wide enough to hold the fixture clock (T0 = 12:00Z) and a
# half-size multiplier so the effect is unmistakable.
HALF_UTC = {"sessions": [
    {"id": "test", "label": "TestWindow", "tz": "UTC", "start": "00:00",
     "end": "23:59", "enabled": True, "risk_mult": 0.5}]}
OFF_UTC = {"sessions": [
    {"id": "test", "label": "TestWindow", "tz": "UTC", "start": "00:00",
     "end": "23:59", "enabled": False, "risk_mult": 0.5}]}


def _bars():
    return series([4020, 4020, (4006, 4006, 3999, 4005),
                   (4005, 4012, 4004, 4011)] + [4011] * 6)


def _run(**vkw):
    v = build_variant(variant_dict(
        sl_rules=NO_RATCHET,
        risk={"default": {"basis": "fixed_cash", "value": 100}}, **vkw))
    rows = [signal_row(signal(), sid=1, at=T0 + dt.timedelta(minutes=1))]
    return v, PortfolioSim(v, _bars()).run(rows)


def test_without_a_trading_hours_block_nothing_is_de_sized():
    """The default must not change the results of any run config written before
    sessions were modelled — a reproducibility claim cannot survive that."""
    v, res = _run()
    assert v.session_windows is None
    assert res.counts["session_desized"] == 0
    assert res.trades[0].planned_risk > 0


def test_an_active_window_de_sizes_by_its_multiplier():
    _v, full = _run()
    _v2, half = _run(trading_hours=HALF_UTC)
    assert half.counts["session_desized"] == 1
    ratio = float(half.trades[0].planned_risk) / float(full.trades[0].planned_risk)
    assert abs(ratio - 0.5) < 0.02          # lot rounding, not exact halving


def test_a_disabled_window_does_not_de_size():
    _v, res = _run(trading_hours=OFF_UTC)
    assert res.counts["session_desized"] == 0


def test_the_multiplier_comes_from_the_shipped_function():
    """Asserted against `trading_hours.sessions.risk_multiplier` itself rather
    than a hand-computed number, so a change to the live definition shows up
    here as a failure instead of as a silently divergent backtest."""
    from beacon_core.trading_hours import sessions as TH
    v = build_variant(variant_dict(trading_hours=HALF_UTC))
    when = T0 + dt.timedelta(minutes=1)
    _active, factor = v.session_context(when)
    assert factor == TH.risk_multiplier(HALF_UTC["sessions"], when)


def test_active_labels_are_exposed_for_the_session_in_rule():
    v = build_variant(variant_dict(trading_hours=HALF_UTC))
    active, _ = v.session_context(T0 + dt.timedelta(minutes=1))
    assert active == ["TestWindow"]


def test_a_session_in_rule_can_now_skip_a_signal():
    """Inert before this: `ctx['sessions']` was never populated, so a
    `session_in` rule was a permanent no-op and a variant expressing one
    measured nothing."""
    v = build_variant(variant_dict(
        trading_hours=HALF_UTC,
        strategies=[{"account_id": None, "source_id": None,
                     "entry_policy": {"entry_style": "limit"},
                     "entry_filters": {"rules": [
                         {"enabled": True, "name": "no-overlap", "mode": "live",
                          "when": {"type": "session_in",
                                   "sessions": ["TestWindow"]},
                          "action": "skip"}]},
                     "exit_policy": {"sl_rules": NO_RATCHET}}]))
    rows = [signal_row(signal(), sid=1, at=T0 + dt.timedelta(minutes=1))]
    res = PortfolioSim(v, _bars()).run(rows)
    assert res.counts["filtration_skip"] == 1


def test_a_session_in_rule_that_does_not_match_lets_the_signal_through():
    v = build_variant(variant_dict(
        trading_hours=HALF_UTC,
        strategies=[{"account_id": None, "source_id": None,
                     "entry_policy": {"entry_style": "limit"},
                     "entry_filters": {"rules": [
                         {"enabled": True, "name": "other", "mode": "live",
                          "when": {"type": "session_in",
                                   "sessions": ["SomeOtherWindow"]},
                          "action": "skip"}]},
                     "exit_policy": {"sl_rules": NO_RATCHET}}]))
    rows = [signal_row(signal(), sid=1, at=T0 + dt.timedelta(minutes=1))]
    res = PortfolioSim(v, _bars()).run(rows)
    assert res.counts["taken"] == 1


def test_session_and_filter_factors_multiply_as_they_do_live():
    v = build_variant(variant_dict(
        trading_hours=HALF_UTC,
        risk={"default": {"basis": "fixed_cash", "value": 100}},
        strategies=[{"account_id": None, "source_id": None,
                     "entry_policy": {"entry_style": "limit"},
                     "entry_filters": {"rules": [
                         {"enabled": True, "name": "half", "mode": "live",
                          "when": {"type": "always"}, "action": "scale",
                          "factor": 0.5}]},
                     "exit_policy": {"sl_rules": NO_RATCHET}}]))
    rows = [signal_row(signal(), sid=1, at=T0 + dt.timedelta(minutes=1))]
    res = PortfolioSim(v, _bars()).run(rows)
    _v, full = _run()
    ratio = float(res.trades[0].planned_risk) / float(full.trades[0].planned_risk)
    assert abs(ratio - 0.25) < 0.02          # 0.5 session x 0.5 filter


def test_the_report_states_whether_sessions_were_modelled():
    """A variant that did not model sessions must not be comparable to one that
    did without the difference being visible."""
    v, res = _run(trading_hours=HALF_UTC)
    rep = metrics.variant_report(res, variant=v, series=_bars())
    assert rep["settings"]["sessions_modelled"] is True
    assert rep["settings"]["n_session_desized"] == 1

    v2, res2 = _run()
    rep2 = metrics.variant_report(res2, variant=v2, series=_bars())
    assert rep2["settings"]["sessions_modelled"] is False


def test_a_broken_session_config_fails_open_at_full_size():
    """Live fails open to x1.0 on a bad lookup rather than mis-sizing. A session
    config must never be able to change exposure by being malformed."""
    v = build_variant(variant_dict(
        trading_hours={"sessions": [{"id": "x", "tz": "Not/AZone",
                                     "start": "nonsense", "enabled": True,
                                     "risk_mult": 0.5}]}))
    active, factor = v.session_context(T0)
    assert factor == 1.0 and active == []
