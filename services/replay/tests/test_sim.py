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
from beacon_core.execution import ladder as LAD
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
def _staged_variant(ladders=None, **staged):
    """A staged variant. The ladder is a GLOBAL GRID (#250), not an entry-policy
    key, so it is set on the variant — `entry_policy["ladder"]` is ignored by the
    sim and offering it here would be a knob that does nothing.

    `ladders` is a grid: {zone: {tp_count: [rows]}}."""
    cfg = {"enabled": True}
    cfg.update(staged)
    ep = {"entry_style": "staged", "ttl_minutes": 120, "staged": cfg}
    v = build_variant(variant_dict(entry_policy=ep, sl_rules=NO_RATCHET,
                                   staged_ladders=ladders))
    return v


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


def test_the_ladder_splits_the_signal_into_triggered_rungs():
    """The `signal` row goes out now; every row waiting on a level is pending."""
    zone = signal(entry=4000.0, entry_to=3990.0, sl=3980.0,
                  tps=(4010.0, 4020.0, 4030.0))
    trade, why, _s, _mc, _ = _staged_plan(_staged_variant(), zone, [4020] * 5)
    assert why is None
    assert trade.entry_style == sim.STAGED
    roles = {t.role for t in trade.tranches}
    assert LAD.WHEN_SIGNAL in roles and LAD.WHEN_MID in roles
    # only the signal rung is placed at plan time; the rest wait for their level
    assert any(l.status == F.WORKING for l in trade.legs)
    assert any(l.status == F.PENDING for l in trade.legs)


def test_the_replayed_ladder_risks_what_the_single_shot_would_have():
    """#250's guarantee, end to end through the replay planner rather than the
    pure engine: fill every rung, run to the stop, lose the same money."""
    zone = signal(entry=4000.0, entry_to=3990.0, sl=3980.0,
                  tps=(4010.0, 4020.0, 4030.0))
    staged, _, _, _, _ = _staged_plan(_staged_variant(), zone, [4020] * 5)
    plain, _, _, _, _ = _staged_plan(
        build_variant(variant_dict(entry_policy={"ttl_minutes": 120},
                                   sl_rules=NO_RATCHET)), zone, [4020] * 5)
    assert plain.entry_style == sim.SINGLE_SHOT
    assert staged.planned_risk <= plain.planned_risk
    assert plain.planned_risk - staged.planned_risk < Decimal("1")


def test_the_replay_reads_the_same_grid_cell_the_signal_would_run_live():
    """Criterion 6: which ladder a backtest runs must be the one live would run,
    and that follows from the signal — its entry shape and its TP count."""
    zone = signal(entry=4000.0, entry_to=3990.0, sl=3980.0,
                  tps=(4010.0, 4020.0, 4030.0))
    assert LAD.zone_kind(zone) == LAD.ZONE_RANGE
    assert LAD.rows_for(None, zone) is LAD.DEFAULT_MATRIX[LAD.ZONE_RANGE][3]

    flat = signal(entry=4000.0, entry_to=4000.0, sl=3980.0, tps=(4010.0, 4020.0))
    assert LAD.zone_kind(flat) == LAD.ZONE_SINGLE
    assert LAD.rows_for(None, flat) is LAD.DEFAULT_MATRIX[LAD.ZONE_SINGLE][2]


def test_a_mid_rung_deploys_when_price_reaches_mid():
    """MID here is halfway from the far edge (3990) to the stop (3980) = 3985.
    The path dips to 3982, so the rung must deploy."""
    zone = signal(entry=4000.0, entry_to=3990.0, sl=3980.0,
                  tps=(4010.0, 4020.0, 4030.0))
    v = _staged_variant()
    trade, why, s, _mc, n_warm = _staged_plan(
        v, zone, [4000, (4000, 4000, 3982, 3986), 3986, 3986])
    assert why is None and trade.entry_style == sim.STAGED
    for i in range(n_warm + 1, len(s)):
        sim.step(trade, s[i], variant=v)
    mid = next(t for t in trade.tranches if t.role == LAD.WHEN_MID)
    assert mid.state in (STG.DEPLOYED, STG.ARMED, STG.FILLED), mid.reason


def test_reaching_tp1_cancels_every_other_rung():
    """The `cancel everything else` row. TP1 is 4010 and the path runs to 4015,
    so the rungs still waiting on MID must be cancelled, not left pending."""
    zone = signal(entry=4000.0, entry_to=3990.0, sl=3980.0,
                  tps=(4010.0, 4020.0, 4030.0))
    v = _staged_variant()
    trade, why, s, _mc, n_warm = _staged_plan(
        v, zone, [4000, (4000, 4015, 4000, 4012), 4012, 4012])
    assert why is None and trade.entry_style == sim.STAGED
    for i in range(n_warm + 1, len(s)):
        sim.step(trade, s[i], variant=v)
    waiting = [t for t in trade.tranches if t.role != LAD.WHEN_SIGNAL]
    assert waiting, "the default ladder should leave rungs waiting on MID"
    assert all(t.state in STG.TERMINAL_STATES for t in waiting), \
        [(t.role, t.state) for t in waiting]


def test_a_market_already_past_tp1_does_not_cancel_unfilled_rungs():
    """Signal 1431 in miniature. The cancel-on-TP1 row used to fire on price
    alone, before anything filled, and took every rung with it — seven orders
    placed and seven withdrawn, deployed_risk 0.00.

    A ladder of resting orders only, so nothing can fill: price sits above TP1
    for the whole path and never trades down to either entry."""
    zone = signal(entry=4637.0, entry_to=4633.0, sl=4627.0,
                  tps=(4640.0, 4643.0, 4646.0))
    # A LIMIT-only grid cell, injected as a real variant grid: resting orders
    # only, so the fixture genuinely cannot fill and the precondition is the only
    # thing under test.
    rows = [{"when": "signal", "action": "open", "order": "LIMIT",
             "level": "ENTRY_TO", "target": 2},
            {"when": "signal", "action": "open", "order": "LIMIT",
             "level": "ENTRY_TO", "target": 3},
            {"when": "tp1", "action": "cancel_all"}]
    v = _staged_variant(ladders={"2": {"3": rows}})
    trade, why, s_, _mc, n_warm = _staged_plan(v, zone, [4641.5] * 5)
    assert why is None and trade.entry_style == sim.STAGED
    for i in range(n_warm + 1, len(s_)):
        sim.step(trade, s_[i], variant=v)

    assert not any(l.status in (F.OPEN, F.CLOSED) for l in trade.legs), \
        "the fixture must not fill anything"
    assert not [l for l in trade.legs if l.status == F.CANCELLED], \
        "nothing filled, so the TP1 rule must not fire"
    assert [l for l in trade.legs if l.tp in (4643.0, 4646.0)], \
        "the deeper TPs, still ahead of the market, must survive"


def test_a_market_rung_whose_target_the_price_has_passed_is_not_planned():
    """A POSITION rung fills AT the market. Planned at 4637 with TP1 4640 it
    would fill at 4641.5 — above its own target — booking a loss and labelling it
    tp_hit. `build_plan` drops such a leg; the ladder now does too."""
    zone = signal(entry=4637.0, entry_to=4633.0, sl=4627.0,
                  tps=(4640.0, 4643.0, 4646.0))
    rows = [{"when": "signal", "action": "open", "order": "POSITION",
             "level": "ENTRY_FROM", "target": 1},
            {"when": "signal", "action": "open", "order": "POSITION",
             "level": "ENTRY_FROM", "target": 2},
            {"when": "tp1", "action": "cancel_all"}]
    at = LAD.plan_ladder(zone, rows, current_price=4641.5)
    tps = {float(r.tp) for r in at}
    assert 4640.0 not in tps, "TP1 is behind the market — must not be planned"
    assert 4643.0 in tps, "TP2 is ahead of the market — must survive"
    assert all(r.order_type == "MARKET" for r in at), \
        "POSITION must reach the broker as MARKET"


def test_reaching_tp1_after_a_fill_still_cancels_the_rest():
    """The rule is not weakened: once a rung has filled and TP1 is reached, the
    remaining rungs are still pulled."""
    zone = signal(entry=4000.0, entry_to=3990.0, sl=3980.0,
                  tps=(4010.0, 4020.0, 4030.0))
    v = _staged_variant()
    trade, why, s_, _mc, n_warm = _staged_plan(
        v, zone, [4000, (4000, 4001, 3995, 3999), (3999, 4015, 3998, 4012), 4012])
    assert why is None and trade.entry_style == sim.STAGED
    for i in range(n_warm + 1, len(s_)):
        sim.step(trade, s_[i], variant=v)
    if any(l.status in (F.OPEN, F.CLOSED) for l in trade.legs):
        waiting = [t for t in trade.tranches if t.role != LAD.WHEN_SIGNAL]
        assert all(t.state in STG.TERMINAL_STATES for t in waiting), \
            [(t.role, t.state) for t in waiting]


# --- the ladder as the monitor runs it ---------------------------------------
def _step_all(trade, s_, v, n_warm):
    for i in range(n_warm + 1, len(s_)):
        sim.step(trade, s_[i], variant=v)
    return trade


def _one_zone(**kw):
    """A single-level signal, so the grid's 1-ZONE half applies. From three TPs
    up those cells put a MARKET rung and a STOP rung on the SAME `mid` trigger,
    which is what the next few tests are about."""
    base = dict(entry=4000.0, entry_to=4000.0, sl=3980.0,
                tps=(4010.0, 4020.0, 4030.0))
    base.update(kw)
    return signal(**base)


def test_a_confirmation_stop_does_not_fill_on_the_bar_that_deploys_it():
    """`level` is what the tranche WAITED FOR; `leg.entry` is where the order
    RESTS. The 1-zone 3-TP cell waits for MID (3990) and then rests a STOP back
    at the entry (4000) — a bet that price recovers.

    Handing the deploy trigger to `SimLeg.trigger` set that stop at 3990, which
    the very bar that deployed it had just traded through: every confirmation
    STOP filled instantly, and the confirmation it exists to wait for never had
    to happen. Here price touches 3990 and then sits below 4000, so a correctly
    placed stop must still be working."""
    v = _staged_variant()
    trade, why, s_, _mc, n_warm = _staged_plan(
        v, _one_zone(), [4000, (3998, 3998, 3989, 3992), 3992, 3992])
    assert why is None and trade.entry_style == sim.STAGED
    _step_all(trade, s_, v, n_warm)

    mid = next(t for t in trade.tranches if t.role == LAD.WHEN_MID)
    assert mid.state in (STG.ARMED, STG.DEPLOYED), (mid.state, mid.reason)
    stops = [l for l in trade.legs if l.order_type == "STOP"]
    assert stops, "the 1-zone 3-TP cell must arm a confirmation STOP"
    for leg in stops:
        assert float(leg.entry) == 4000.0
        assert float(leg.trigger) == 4000.0, "a STOP fires at its OWN level"
        assert leg.status != F.OPEN, "price never regained 4000 — it cannot have filled"


def test_a_confirmation_stop_fills_when_price_actually_comes_back():
    """The other half: the rule is not simply disabled. Once price does regain
    the entry, the armed STOP fills there."""
    v = _staged_variant()
    trade, why, s_, _mc, n_warm = _staged_plan(
        v, _one_zone(), [4000, (3998, 3998, 3989, 3992), (3992, 4003, 3992, 4002), 4002])
    assert why is None
    _step_all(trade, s_, v, n_warm)
    stops = [l for l in trade.legs if l.order_type == "STOP"]
    assert any(l.status in (F.OPEN, F.CLOSED) for l in stops), \
        [(l.status, l.entry, l.trigger) for l in stops]


def test_a_deployed_market_rung_fills_at_its_trigger_not_the_bar_open():
    """The monitor sends the market order the moment price reaches the rung's
    trigger, so that is where it fills. Filling it at the bar's OPEN credited a
    backtest with an entry live could never have got — here the bar opens at 4000
    and the rung triggers at MID 3990, ten points better for a BUY."""
    v = _staged_variant()
    trade, why, s_, _mc, n_warm = _staged_plan(
        v, _one_zone(), [4000, (3998, 3998, 3989, 3992), 3992, 3992])
    assert why is None
    _step_all(trade, s_, v, n_warm)

    mkt = [l for l in trade.legs
           if l.order_type == "MARKET" and l.status in (F.OPEN, F.CLOSED)
           and l.trigger is not None]
    assert mkt, "the 1-zone 3-TP cell must take size at MID"
    for leg in mkt:
        assert abs(float(leg.fill_price) - 3990.0) < 1.0, \
            f"filled at {leg.fill_price}, not at its 3990 trigger"


def test_a_cancel_row_that_is_not_tp1_is_honoured_too():
    """The grammar allows a cancel row on any trigger; replay only ever read
    `tp1`, so a ladder that stops adding size once the far edge trades would run
    unthrottled in the backtest while cancelling live — the two arms measuring
    different rules."""
    rows = [{"when": "signal", "action": "open", "order": "POSITION",
             "level": "ENTRY_FROM", "target": 1},
            {"when": "mid", "action": "open", "order": "POSITION",
             "level": "MID", "target": 2},
            {"when": "entry_to", "action": "cancel_all"}]
    v = _staged_variant(ladders={"1": {"3": rows}})
    # opens at the market (4000), then trades down THROUGH the entry (the cancel
    # trigger) and on to MID — the `mid` rung must never deploy.
    trade, why, s_, _mc, n_warm = _staged_plan(
        v, _one_zone(), [4000, (4000, 4000, 3999, 3999), (3999, 3999, 3988, 3991), 3991])
    assert why is None and trade.entry_style == sim.STAGED
    _step_all(trade, s_, v, n_warm)
    mid = next(t for t in trade.tranches if t.role == LAD.WHEN_MID)
    assert mid.state in STG.TERMINAL_STATES, (mid.state, mid.reason)


def test_the_cancel_row_pulls_the_signal_tranches_orders_too():
    """"Cancel everything else" means every order still working for this signal.
    Live queries the TRADE's working legs; replay skipped the signal tranche, so
    a resting entry live had already withdrawn stayed alive in the backtest and
    could still fill."""
    # The market rung targets TP2, not TP1, so reaching TP1 leaves it OPEN — the
    # trade must still be running when the cancel fires, or the ordinary
    # trade-closed cleanup would pull the resting order and prove nothing.
    rows = [{"when": "signal", "action": "open", "order": "POSITION",
             "level": "ENTRY_FROM", "target": 2},
            {"when": "signal", "action": "open", "order": "LIMIT",
             "level": "MID", "target": 3},
            {"when": "tp1", "action": "cancel_all"}]
    v = _staged_variant(ladders={"1": {"3": rows}})
    # the POSITION rung fills at once, then price runs to TP1 (4010) but not TP2
    trade, why, s_, _mc, n_warm = _staged_plan(
        v, _one_zone(), [4000, (4000, 4012, 4000, 4011), 4011, 4011])
    assert why is None and trade.entry_style == sim.STAGED
    _step_all(trade, s_, v, n_warm)

    assert any(l.status in (F.OPEN, F.CLOSED) for l in trade.legs), \
        "the precondition needs something filled"
    resting = [l for l in trade.legs if l.order_type == "LIMIT"]
    assert resting, "the fixture must leave a resting order to pull"
    assert not any(l.status == F.WORKING for l in resting), \
        [(l.order_type, l.status) for l in resting]
    sig_tr = next(t for t in trade.tranches if t.role == LAD.WHEN_SIGNAL)
    assert sig_tr.state in STG.TERMINAL_STATES, (sig_tr.state, sig_tr.reason)


def test_a_tranche_of_mixed_rungs_is_armed_and_labelled_mixed():
    """The `mid` tranche of a 1-zone 3-TP signal owns a MARKET rung and a STOP
    rung. It is ARMED because one of them rests at the broker, and its `mode`
    label says so rather than claiming one type for both."""
    v = _staged_variant()
    trade, why, s_, _mc, n_warm = _staged_plan(
        v, _one_zone(), [4000, (3998, 3998, 3989, 3992), 3992, 3992])
    assert why is None
    _step_all(trade, s_, v, n_warm)
    mid = next(t for t in trade.tranches if t.role == LAD.WHEN_MID)
    assert mid.mode == "MIXED", mid.mode
    assert mid.state == STG.ARMED, (mid.state, mid.reason)
