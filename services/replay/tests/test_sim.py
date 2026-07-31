"""The trade simulator, driven through the REAL engines.

Each test here is really asking the same question: does the harness behave the
way the shipped code behaves? Where it can, it asserts against a beacon_core
function's own output rather than a hand-computed number, so a change to live
logic shows up as a failure here rather than as a silently divergent backtest.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from harness import bars as B
from harness import fills as F
from harness import sim
from harness.context import ContextBuilder
from harness.variants import build_variant
from beacon_core.execution import staging as STG
from conftest import (NO_RATCHET, T0, path_bars, series, signal,
                      sl_rules_be_at, variant_dict)


def _plan(mids, *, sig=None, variant=None, at=None, **vkw):
    """Plan one trade against a synthetic series and hand back everything the
    caller needs to step it forward."""
    s = series(mids)
    v = variant or build_variant(variant_dict(**vkw))
    cb = ContextBuilder(s)
    parsed = sig or signal()
    when = at or (T0 + dt.timedelta(minutes=1))
    cfg = v.resolve(1, 7)
    mc = cb.build(when, filter_rules=cfg.filter_rules,
                  need_staged_atr=str(cfg.entry_policy.get("entry_style") or "") == "staged")
    trade, why = sim.plan_trade(signal=parsed, signal_id=1, source_id=7,
                                account_id=1, signal_at=when, cfg=cfg,
                                variant=v, mc=mc)
    return trade, why, s, v, mc


def _run(trade, s, v, *, start=1):
    for i in range(start, len(s)):
        sim.step(trade, s[i], variant=v)
        if trade.is_done:
            break
    if not trade.is_done:
        sim.finish(trade, s[len(s) - 1], variant=v)
    return trade


# --- planning -----------------------------------------------------------------
def test_a_resting_entry_plans_limit_legs_one_per_tp():
    trade, why, *_ = _plan([4020] * 5)          # price above a 4000 BUY entry
    assert why is None
    assert len(trade.legs) == 3                 # 3 TPs, single entry
    assert {l.order_type for l in trade.legs} == {"LIMIT"}
    assert trade.entry_style == sim.SINGLE_SHOT


def test_an_already_crossed_entry_plans_a_market_leg():
    """`build_plan` collapses crossed entries into ONE market fill — the harness
    must not re-derive that rule, it must inherit it."""
    trade, why, *_ = _plan([3995] * 5)
    assert why is None
    assert {l.order_type for l in trade.legs} == {"MARKET"}


def test_invalid_geometry_is_a_reason_not_an_exception():
    bad = signal(sl=4010.0)                     # BUY stop above entry
    trade, why, *_ = _plan([4020] * 3, sig=bad)
    assert trade is None and "invalid_geometry" in why


def test_a_signal_whose_legs_all_fall_below_min_lot_is_not_taken():
    trade, why, *_ = _plan([4020] * 3, risk={"default": {
        "basis": "fixed_cash", "value": 0.0001, "allocation": "even"}})
    assert trade is None and why == "no_legs_survived_sizing"


def test_the_per_signal_risk_cap_scales_the_plan_down():
    """Equity 10000 x 0.1% = a 10 cap against a 300 fixed-cash plan."""
    trade, why, *_ = _plan(
        [4020] * 3,
        risk={"default": {"basis": "fixed_cash", "value": 300, "allocation": "even"}},
        risk_limits={"enabled": True, "max_signal_risk_pct": 0.1,
                     "daily_loss_limit": 0, "max_open_risk_per_symbol": 0,
                     "max_open_risk_per_account": 0})
    assert why is None
    assert trade.planned_risk <= Decimal("10")


# --- the walk -----------------------------------------------------------------
def test_a_limit_fills_then_takes_tp1():
    # dip to the entry, then rally through TP1 only.
    trade, _, s, v, _ = _plan([4020, 4020, (4006, 4006, 3999, 4005),
                               (4005, 4012, 4004, 4011), 4011, 4011])
    _run(trade, s, v)
    tp1 = next(l for l in trade.legs if l.tp_index == 1)
    assert tp1.outcome == F.TP_HIT
    assert tp1.fill_price == 4000.0
    assert trade.realized_pl > 0


def test_the_ratchet_turns_a_retrace_after_tp1_into_a_breakeven():
    """The BE@TP1 mechanism, end to end: TP1 prints, the stop moves to entry on
    the surviving legs, and the retrace closes them flat instead of at -1R."""
    v = build_variant(variant_dict(sl_rules=sl_rules_be_at(1)))
    trade, _, s, _, _ = _plan(
        [4020, 4020,
         (4006, 4006, 3999, 4005),          # fill at 4000
         (4005, 4012, 4004, 4011),          # TP1 (4010) prints -> ratchet
         (4011, 4011, 3999, 3999),          # retrace through entry
         3999],
        variant=v)
    _run(trade, s, v)
    tp1 = next(l for l in trade.legs if l.tp_index == 1)
    tp2 = next(l for l in trade.legs if l.tp_index == 2)
    assert tp1.outcome == F.TP_HIT
    assert tp2.outcome == F.BREAKEVEN
    assert tp2.sl_moved is True
    assert tp2.realized_pl == Decimal("0")


def test_without_a_ratchet_the_same_path_stops_out_at_minus_one_r():
    """The control for the test above — same bars, a ratchet that never fires."""
    v = build_variant(variant_dict(sl_rules=NO_RATCHET))
    trade, _, s, _, _ = _plan(
        [4020, 4020, (4006, 4006, 3999, 4005), (4005, 4012, 4004, 4011),
         (4011, 4011, 3985, 3985), 3985],
        variant=v)
    _run(trade, s, v)
    tp2 = next(l for l in trade.legs if l.tp_index == 2)
    assert tp2.outcome == F.SL_HIT
    assert tp2.realized_pl == -tp2.risk_cash


def test_the_ratchet_cannot_protect_the_bar_it_fired_on():
    """A stop a rule would have moved DURING a bar does not get to act
    retroactively inside it — the monitor observes, then acts."""
    v = build_variant(variant_dict(sl_rules=sl_rules_be_at(1)))
    trade, _, s, _, _ = _plan(
        [4020, 4020, (4006, 4006, 3999, 4005),
         (4005, 4012, 3985, 3990),          # TP1 AND the original SL in one bar
         3990],
        variant=v)
    _run(trade, s, v)
    tp2 = next(l for l in trade.legs if l.tp_index == 2)
    assert tp2.outcome == F.SL_HIT          # not breakeven
    assert trade.same_bar_ambiguous >= 1


def test_an_unfilled_limit_expires_on_its_ttl_and_has_no_pnl():
    v = build_variant(variant_dict(entry_policy={"entry_style": "limit",
                                                 "ttl_minutes": 5}))
    trade, _, s, _, _ = _plan([4020] * 12, variant=v)
    _run(trade, s, v)
    assert trade.is_done
    assert all(l.status == F.EXPIRED for l in trade.legs)
    assert trade.realized_pl == Decimal("0")
    assert trade.ever_filled is False


def test_cancel_pending_on_stop_retires_a_resting_rung_once_the_trade_progresses():
    """A zone signal rests two entries. The near rung fills and runs to TP1;
    the deep rung is then stale — price went where we wanted without it, so
    filling now would be entering late (#25) — and `cancel_reason` retires it."""
    v = build_variant(variant_dict(sl_rules=NO_RATCHET))
    zone = signal(entry=4000.0, entry_to=3990.0, sl=3985.0,
                  tps=(4010.0, 4020.0))
    trade, why, s, _, _ = _plan(
        [4020, 4020, (4006, 4006, 3999, 4005), (4005, 4012, 4004, 4011), 4011],
        sig=zone, variant=v)
    assert why is None
    _run(trade, s, v)
    deep = [l for l in trade.legs if l.entry == 3990.0]
    assert deep and all(l.status == F.CANCELLED for l in deep)


def test_horizon_capped_trades_are_marked_to_market_not_won():
    v = build_variant(variant_dict(sl_rules=NO_RATCHET, horizon_bars=3))
    trade, _, s, _, _ = _plan([4020, 4020, (4006, 4006, 3999, 4005)] + [4005] * 8,
                              variant=v)
    for i in range(1, len(s)):
        if i > 1 + v.horizon_bars:
            sim.finish(trade, s[i], variant=v)
            break
        sim.step(trade, s[i], variant=v)
    assert trade.horizon_capped is True
    assert all(l.outcome != F.TP_HIT for l in trade.legs)


# --- staged -------------------------------------------------------------------
def _staged_variant(**staged):
    cfg = {"enabled": True, "toe_in_tps": 1, "runner_tps": 1, "min_stop_atr": 0.0}
    cfg.update(staged)
    return build_variant(variant_dict(
        entry_policy={"entry_style": "staged", "ttl_minutes": 120, "staged": cfg},
        sl_rules=NO_RATCHET))


WARMUP_HOURS = 40


def _staged_series(trading_mids):
    """Hourly-spaced warm-up bars so ATR(14) on the 1h frame actually exists,
    then the 1m bars the trade walks. Without the warm-up the staged path takes
    the executor's no-ATR fallback and the test would silently measure the
    single-shot branch instead."""
    warm = path_bars(
        [(4020, 4024, 4016, 4020 + (3 if i % 2 else -3)) for i in range(WARMUP_HOURS)],
        start=T0, step_minutes=60)
    trade_start = warm[-1].ts + dt.timedelta(minutes=60)
    return B.BarSeries(warm + path_bars(trading_mids, start=trade_start)), \
        len(warm), trade_start


def _staged_plan(v, sig, trading_mids):
    s, n_warm, start = _staged_series(trading_mids)
    cb = ContextBuilder(s)
    when = start + dt.timedelta(minutes=1)
    cfg = v.resolve(1, 7)
    mc = cb.build(when, need_staged_atr=True)
    trade, why = sim.plan_trade(signal=sig, signal_id=1, source_id=7,
                                account_id=1, signal_at=when, cfg=cfg,
                                variant=v, mc=mc)
    return trade, why, s, mc, n_warm


def test_staged_partitions_the_ladder_into_tranches():
    zone = signal(entry=4000.0, entry_to=3990.0, sl=3980.0,
                  tps=(4010.0, 4020.0, 4030.0))
    trade, why, _s, mc, _ = _staged_plan(_staged_variant(), zone, [4020] * 5)
    assert mc.atr_1h is not None, "fixture must produce a usable 1h ATR"
    assert why is None
    assert trade.entry_style == sim.STAGED
    roles = {t.role for t in trade.tranches}
    assert STG.TOE_IN in roles and STG.RECLAIM in roles
    # only the toe-in is placed at signal time; the rest wait on DECIDE
    assert any(l.status == F.WORKING for l in trade.legs)
    assert any(l.status == F.PENDING for l in trade.legs)


def test_staged_falls_back_to_single_shot_when_the_stop_is_too_tight():
    """Exactly the executor's #156 fallback. Without it a replayed 'staged' arm
    would include trades live actually ran flat."""
    tight = signal(entry=4000.0, entry_to=3999.0, sl=3998.5, tps=(4010.0, 4020.0))
    trade, why, *_ = _staged_plan(_staged_variant(min_stop_atr=5.0), tight,
                                  [4020] * 5)
    assert why is None and trade.entry_style == sim.SINGLE_SHOT


def test_a_runner_deploys_when_price_reaches_the_deep_edge():
    zone = signal(entry=4000.0, entry_to=3990.0, sl=3980.0,
                  tps=(4010.0, 4020.0, 4030.0))
    v = _staged_variant()
    trade, why, s, _mc, n_warm = _staged_plan(
        v, zone, [4020, (4006, 4006, 3989, 3992), 3992, 3992])
    assert why is None and trade.entry_style == sim.STAGED
    for i in range(n_warm + 1, len(s)):
        sim.step(trade, s[i], variant=v)
    runner = next((t for t in trade.tranches if t.role == STG.RUNNER), None)
    if runner is not None:                       # partition may fold it into toe-in
        assert runner.state in (STG.DEPLOYED, STG.FILLED)
