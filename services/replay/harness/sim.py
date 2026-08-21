"""The trade simulator: plan a signal with the REAL engines, then walk it bar by
bar (#169 §2).

FIDELITY IS THE WHOLE POINT. A harness that reimplements the logic tests a
different bot and is worse than useless, so every behavioural decision below is
delegated to the shipped function:

    execution/planner.build_plan          entry model, chase guard, TP geometry
    execution/ladder.plan_ladder          the rungs the operator's table defines
    execution/ladder.size_ladder          pre-sizing to the single-shot total
    execution/ladder.reached              has price arrived at a rung's level
    execution/strategy.evaluate_filter_rules   the filtration pillar
    execution/strategy.cancel_reason      what retires a resting order (#161)
    risk/sizing.size_legs / cap_total_risk     lots and the per-signal cap
    strategy/rules.evaluate               the SL ratchet
    strategy/rules.levels_reached / entry_basis

What lives HERE and nowhere else is the part live has no equivalent of: turning
a 1m bid/ask bar into fills and exits (`fills.py`), and sequencing those within
a bar. Any divergence needed for simulation is a divergence in the harness — it
is never a fork of core.

WITHIN-BAR ORDER, fixed and conservative:

    1. staged DECIDE (deploy / expire tranches)
    2. near-miss tracking on still-resting orders (#185 diagnostic, no effect)
    3. TTL expiry of still-resting orders
    4. entry fills
    5. exits — ADVERSE FIRST, same-bar TP+SL scored as the stop
    6. MFE update + SL ratchet, effective from the NEXT bar
    7. cancel_pending_on_stop

Step 6 lands after step 5 on purpose: a stop that a rule would have ratcheted
during this bar does not get to protect the position retroactively within it.
Live, the monitor observes and then acts; the same is true here.

THAT CHOICE HAS A MEASURED COST (#185, defect B). 21 of the validation gate's 72
disagreements are `sim=tp_hit` where live went `breakeven`: live ratcheted
mid-minute and the retrace inside that same minute took the position out, while
the simulation still had the original stop and let the trade run on to target.
So the harness is optimistic on exits, and it is the largest single contributor
to the residual +0.060R mean delta.

It is a modelling choice with a real cost, not a bug — so it is now SELECTABLE
rather than assumed. `ratchet_timing: "same_bar"` adds a second exit pass after
step 6: the stop moves on this bar's favourable extreme and is then re-tested
against this bar's adverse extreme. The default stays `next_bar`, so nothing
changes until someone runs the gate both ways and keeps the winner. Whichever is
in force is recorded on the result, because two variants that differ on it are
not comparable.

PURE — stdlib + beacon_core. No DB, no clock, no broker.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

from beacon_core.execution import staging as STG
from beacon_core.execution import ladder as LAD
from beacon_core.execution import strategy as ST
from beacon_core.execution.planner import FanoutPlan, build_plan, validate_signal
from beacon_core.parsing.models import ParsedSignal
from beacon_core.risk.sizing import cap_total_risk, plan_total_risk, size_legs
from beacon_core.strategy.rules import (PositionCtx, entry_basis, evaluate,
                                        levels_reached)

from . import bars as B
from . import fills as F
from .context import MarketContext
from .variants import RATCHET_SAME_BAR, ResolvedConfig, Variant

# Roles whose legs are NOT placed at signal time — the monitor deploys them when
# the DECIDE engine says so.
# Every rung except the one that fires when the signal arrives waits for a
# level, so it is not placed at plan time (#250).

SINGLE_SHOT = "single_shot"
STAGED = "staged"


@dataclass
class Tranche:
    """Mirrors `db.models.StagedTranche`: the DECIDE state plus its OWN clock. A
    reclaim armed late must not expire on the trade's clock (#129)."""
    role: str
    state: str = STG.PENDING
    state_since: Optional[dt.datetime] = None
    mode: Optional[str] = None
    trigger_level: Optional[float] = None
    reason: str = ""
    leg_indices: List[int] = field(default_factory=list)


@dataclass
class SimTrade:
    """One signal on one account under one variant — the fanout, its state, and
    the frozen config it runs under. `sl_rules` is SNAPSHOT at entry exactly as
    `trades.sl_rules` is live, so an exit ladder is immune to anything the run
    does later."""
    signal_id: int
    source_id: Optional[int]
    account_id: int
    symbol: str
    direction: str
    signal_at: dt.datetime
    legs: List[F.SimLeg]
    tp_ladder: Dict[int, Decimal]
    sl_rules: List[dict]
    cancel_pending_on_stop: bool
    entry_ttl_minutes: int
    planned_risk: Decimal
    entry_style: str = SINGLE_SHOT
    initial_sl: Optional[float] = None
    mfe: Optional[float] = None
    staged_cfg: Optional[dict] = None
    staged_geo: Optional[dict] = None            # {near, deep, atr}
    tranches: List[Tranche] = field(default_factory=list)
    max_adverse_beyond_deep: float = 0.0
    closed_tp_hits: set = field(default_factory=set)
    same_bar_ambiguous: int = 0
    # Legs the TTL retired on a bar they would have filled in (#185 diagnostic).
    expired_on_fillable_bar: int = 0
    horizon_capped: bool = False
    strategy_label: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    # --- state ---------------------------------------------------------------
    @property
    def live_legs(self) -> List[F.SimLeg]:
        return [l for l in self.legs if l.is_live]

    @property
    def open_legs(self) -> List[F.SimLeg]:
        return [l for l in self.legs if l.is_open]

    @property
    def is_done(self) -> bool:
        return not self.live_legs

    @property
    def realized_pl(self) -> Decimal:
        return sum((l.realized_pl for l in self.legs if l.realized_pl is not None),
                   Decimal("0"))

    @property
    def ever_filled(self) -> bool:
        return any(l.fill_price is not None for l in self.legs)


# ============================ planning ========================================
def plan_trade(*, signal: ParsedSignal, signal_id: int, source_id, account_id: int,
               signal_at: dt.datetime, cfg: ResolvedConfig, variant: Variant,
               mc: MarketContext) -> tuple:
    """Build and size the fanout for one (signal, account). Returns
    `(SimTrade | None, reason)`; a None trade with a reason is a signal the
    variant did NOT take, which is data, not an error — the counterfactual
    "what did the ones we skipped do?" is a first-class output (§6).
    """
    ok, why = validate_signal(signal)
    if not ok:
        return None, f"invalid_geometry: {why}"

    acct = variant.account(account_id)
    if acct is None:
        return None, "unknown_account"

    ep = cfg.entry_policy
    max_tp_pct = _dec(ep.get("max_tp_distance_pct", "0.5"))
    honor_hint = bool(ep.get("honor_market_hint", True))
    chase_r = _dec(ep.get("chase_tolerance_r", "0.25"))
    chase_atr = _dec(ep.get("chase_tolerance_atr", "0"))
    beyond = str(ep.get("beyond_tolerance", "limit"))

    entry_style = SINGLE_SHOT
    staged_cfg = staged_geo = None
    want_staged = str(ep.get("entry_style") or "") == STAGED
    ladder_rows = None
    if want_staged:
        staged_cfg = STG.staged_config(ep.get("staged"))
        # Same grid live reads, so a backtested ladder is the ladder that runs.
        # A variant may carry its own `staged_ladders` to test a different grid;
        # absent, it is the shipped default.
        ladder_rows = LAD.rows_for(
            LAD.matrix_with_defaults(getattr(variant, "staged_ladders", None)), signal)
        # Same guards, and the same whole-signal chase skip, as the control arm —
        # a replayed staged arm that took signals live skipped would be comparing
        # different populations (#152/#155).
        _control = build_plan(
            signal, current_price=mc.current_price,
            candle_high=mc.candle_high, candle_low=mc.candle_low,
            min_stop_distance=variant.min_stop_distance,
            max_tp_distance_pct=max_tp_pct if max_tp_pct > 0 else None,
            honor_market_hint=honor_hint, chase_tolerance_r=chase_r,
            chase_tolerance_atr=chase_atr, beyond_tolerance=beyond)
        if not _control.legs:
            return None, "chase_guard_skip"
        legs = LAD.plan_ladder(
            signal, ladder_rows,
            max_tp_distance_pct=max_tp_pct if max_tp_pct > 0 else None,
            min_stop_distance=variant.min_stop_distance)
        # The SAME fallback the executor takes: no row the signal can support (no
        # such TP, or geometry that puts a rung on the wrong side of its own stop)
        # and the account runs single-shot. Without it a replayed "staged" arm
        # would silently include trades live ran flat.
        if not legs:
            want_staged = False
            ladder_rows = None
        else:
            plan = FanoutPlan(symbol=signal.symbol, direction=signal.direction,
                              order_type="LIMIT", legs=legs)
            entry_style = STAGED
            staged_geo = {"mid": float(LAD.mid_level(signal.entry_to, signal.sl)),
                          "tp1": float(signal.tps[0]) if signal.tps else None,
                          "cancel_on": LAD.cancel_rows(ladder_rows)}
    if entry_style == SINGLE_SHOT:
        plan = build_plan(
            signal, current_price=mc.current_price,
            candle_high=mc.candle_high, candle_low=mc.candle_low,
            min_stop_distance=variant.min_stop_distance,
            max_tp_distance_pct=max_tp_pct if max_tp_pct > 0 else None,
            honor_market_hint=honor_hint, chase_tolerance_r=chase_r,
            chase_tolerance_atr=chase_atr, beyond_tolerance=beyond)
        if not plan.legs:
            return None, "chase_guard_skip"

    if ladder_rows is not None:
        # #250: pre-sized to what the ORDINARY fanout would have risked on this
        # same signal, so a fully-filled ladder loses the same money the
        # single-shot entry would have. Measured, not assumed — the same call the
        # executor makes, so a backtested ladder and a live one stake the same.
        _target = LAD.single_shot_risk(
            signal, current_price=mc.current_price, equity=acct.equity,
            risk=cfg.risk, instrument=variant.instrument, fx_factor=acct.fx_factor,
            candle_high=mc.candle_high, candle_low=mc.candle_low,
            min_stop_distance=variant.min_stop_distance,
            max_tp_distance_pct=max_tp_pct if max_tp_pct > 0 else None,
            honor_market_hint=honor_hint, chase_tolerance_r=chase_r,
            chase_tolerance_atr=chase_atr, beyond_tolerance=beyond)
        LAD.size_ladder(plan.legs, budget=_target,
                        instrument=variant.instrument, fx_factor=acct.fx_factor)
    else:
        size_legs(plan.legs, equity=acct.equity, risk=cfg.risk,
                  instrument=variant.instrument, fx_factor=acct.fx_factor)

    # Per-signal risk cap (#78) — the same scale-down the executor applies before
    # anything reaches the risk-limit check.
    cap_pct = _dec((variant.risk_limits or {}).get("max_signal_risk_pct", 0) or 0)
    if cap_pct > 0:
        cap = acct.equity * cap_pct / Decimal(100)
        if cap > 0 and plan_total_risk(plan.legs) > cap:
            cap_total_risk(plan.legs, cap=cap, instrument=variant.instrument,
                           fx_factor=acct.fx_factor)

    valid = plan.valid_legs
    if not valid:
        return None, "no_legs_survived_sizing"

    tp_ladder = {l.tp_index: Decimal(str(l.tp)) for l in plan.legs}
    initial_sl = float(signal.sl)
    sim_legs: List[F.SimLeg] = []
    for l in valid:
        deferred = bool(l.tranche) and l.tranche != LAD.WHEN_SIGNAL
        sim_legs.append(F.SimLeg(
            tp_index=l.tp_index, order_type=l.order_type, entry=float(l.entry),
            tp=float(l.tp), sl=float(l.sl), initial_sl=initial_sl,
            lot=l.lot or Decimal("0"), risk_cash=l.risk_cash or Decimal("0"),
            tranche=l.tranche,
            trigger=None if l.trigger is None else float(l.trigger),
            status=F.PENDING if deferred else F.WORKING,
            placed_at=None if deferred else signal_at))

    trade = SimTrade(
        signal_id=signal_id, source_id=source_id, account_id=account_id,
        symbol=signal.symbol, direction=signal.direction, signal_at=signal_at,
        legs=sim_legs, tp_ladder=tp_ladder, sl_rules=list(cfg.sl_rules),
        cancel_pending_on_stop=cfg.cancel_pending_on_stop,
        entry_ttl_minutes=cfg.ttl_minutes,
        planned_risk=plan_total_risk(plan.legs),
        entry_style=entry_style, initial_sl=initial_sl,
        staged_cfg=staged_cfg, staged_geo=staged_geo)
    if entry_style == STAGED:
        trade.tranches = _build_tranches(sim_legs, signal_at)
    return trade, None


def _build_tranches(legs: List[F.SimLeg], at: dt.datetime) -> List[Tranche]:
    """One tranche per ladder trigger, in `ladder.WHENS` order so the step loop is
    deterministic regardless of how deep the TP ladder goes (#250)."""
    by_role: Dict[str, Tranche] = {}
    for i, leg in enumerate(legs):
        role = leg.tranche
        if role is None:
            continue
        t = by_role.get(role)
        if t is None:
            t = Tranche(role=role, state_since=at)
            # The `signal` rung is placed at signal time, so it is already
            # deployed when the trade starts existing.
            t.state = STG.DEPLOYED if role == LAD.WHEN_SIGNAL else STG.PENDING
            by_role[role] = t
        t.leg_indices.append(i)
    return [by_role[r] for r in LAD.WHENS if r in by_role]


# ============================ the bar step ====================================
def step(trade: SimTrade, bar: B.Bar, *, variant: Variant) -> None:
    """Advance `trade` through one bar. Idempotent for a finished trade."""
    if trade.is_done:
        return
    slip = variant.slippage_points
    if trade.entry_style == STAGED:
        _staged_step(trade, bar)
    _track_approach(trade, bar)
    _expire_working(trade, bar)
    _fill_working(trade, bar, slip)
    _resolve_exits(trade, bar, variant, slip)
    _ratchet(trade, bar, variant)
    if variant.ratchet_timing == RATCHET_SAME_BAR:
        # #185 defect B: re-test the freshly-moved stops against THIS bar's
        # adverse extreme. Idempotent for a stop that did not move — the first
        # pass already tested it — so the ONLY legs this can close are the ones
        # a ratchet just protected, which is precisely the population live took
        # a breakeven on and `next_bar` lets run to target.
        #
        # Deliberately a SECOND pass rather than ratcheting before exits: the
        # leg whose own TP armed the rule must still take that TP, exactly as
        # the broker's resting TP order does live. Ratcheting first would stop
        # it out at break-even on the bar its target was reached, which is a
        # different (and wrong) model, not a more conservative one.
        _resolve_exits(trade, bar, variant, slip)
    _cancel_pending(trade, bar)


def finish(trade: SimTrade, bar: Optional[B.Bar], *, variant: Variant) -> None:
    """The horizon ran out. Still-open positions are marked to market and
    labelled `horizon`; still-resting orders are expired. Both are counted — a
    variant whose losers are simply never resolved must not read as patient."""
    if bar is not None:
        for leg in trade.open_legs:
            F.close_at_horizon(leg, trade.direction, bar)
            acct = variant.account(trade.account_id)
            F.settle(leg, trade.direction,
                     value_per_point=variant.instrument.value_per_point,
                     fx_factor=acct.fx_factor if acct else Decimal("1"))
            trade.horizon_capped = True
    ts = bar.ts if bar is not None else trade.signal_at
    for leg in trade.legs:
        if leg.is_working or leg.is_pending:
            F.expire(leg, ts, "horizon") if leg.is_working else _drop_pending(leg, ts)


def _drop_pending(leg: F.SimLeg, ts) -> None:
    leg.status = F.EXPIRED
    leg.outcome = F.EXPIRED_OUT
    leg.closed_at = ts
    leg.note(ts, "never_deployed")


# --- 1. staged DECIDE ---------------------------------------------------------
def _staged_step(trade: SimTrade, bar: B.Bar) -> None:
    """Run the shipped LADDER for every unresolved rung (#250).

    The monitor feeds it a live snapshot each tick; here the snapshot is the bar.
    The entry-side price input is the bar's ADVERSE EXTREME, not its close: a
    monitor polling every few seconds sees the extreme, and reading the close
    would under-deploy exactly the rungs a resting level is placed to catch, so
    the replayed ladder would diverge from the live one it stands in for.

    `ladder.reached` / `reached_target` are the same predicates the monitor calls,
    so a rung triggers here on the same price it triggers on live."""
    geo = trade.staged_geo
    if not geo:
        return
    adverse_px = B.adverse_extreme(trade.direction, bar)

    # A cancel row first: reaching TP1 pulls every other rung, and a rung that
    # would otherwise deploy on this same bar must not slip out ahead of it.
    tp1 = geo.get("tp1")
    if tp1 is not None and LAD.WHEN_TP1 in (geo.get("cancel_on") or ()):
        # The FAVOURABLE extreme against reached_TARGET: a TP touched inside the
        # minute is a touch, and TP1 sits in the winning direction, so the
        # entry-side `reached` would be true from the very first bar.
        if LAD.reached_target(trade.direction,
                              B.favourable_extreme(trade.direction, bar), tp1):
            for tr in trade.tranches:
                if tr.role != LAD.WHEN_SIGNAL and tr.state not in STG.TERMINAL_STATES:
                    tr.reason = "ladder: TP1 reached"
                    _resolve_tranche(trade, tr, STG.CANCELLED, bar.ts)
            return

    for tr in trade.tranches:
        if tr.role == LAD.WHEN_SIGNAL or tr.state != STG.PENDING:
            continue
        level = _rung_level(trade, tr)
        if level is None:
            continue
        if LAD.reached(trade.direction, adverse_px, level):
            tr.reason = f"ladder: reached {level}"
            _deploy(trade, tr, level, bar)
        elif trade.entry_ttl_minutes and _minutes(tr.state_since, bar.ts) > float(
                trade.entry_ttl_minutes):
            # A rung cannot wait forever — the trade stays alive while any rung is
            # pending, so it answers to the same entry-TTL clock a resting entry
            # order does. Mirrors the monitor exactly.
            tr.reason = "ladder: level never reached"
            _resolve_tranche(trade, tr, STG.EXPIRED, bar.ts)


def _rung_level(trade: SimTrade, tr: Tranche):
    """The price a rung waits for — carried on its legs at plan time."""
    for i in tr.leg_indices:
        trig = trade.legs[i].trigger
        if trig is not None:
            return float(trig)
    return None


def _deploy(trade: SimTrade, tr: Tranche, level: float, bar: B.Bar) -> None:
    """Place a rung's orders. A STOP rung goes to ARMED (resting at the broker);
    anything else to DEPLOYED. Each leg's TTL clock starts now (#158)."""
    mode = None
    for i in tr.leg_indices:
        leg = trade.legs[i]
        if not leg.is_pending:
            continue
        mode = leg.order_type or "LIMIT"
        leg.deploy(bar.ts, mode, float(leg.entry),
                   trigger=float(level) if mode == LAD.ORDER_STOP else None)
    tr.mode = mode
    tr.trigger_level = float(level)
    tr.state = STG.ARMED if mode == LAD.ORDER_STOP else STG.DEPLOYED
    tr.state_since = bar.ts


def _resolve_tranche(trade: SimTrade, tr: Tranche, state: str, ts) -> None:
    tr.state = state
    tr.state_since = ts
    for i in tr.leg_indices:
        leg = trade.legs[i]
        if leg.is_pending:
            _drop_pending(leg, ts)
        elif leg.is_working:
            F.expire(leg, ts, state)


def _minutes(since: Optional[dt.datetime], now: dt.datetime) -> float:
    if since is None:
        return 0.0
    return max(0.0, (now - since).total_seconds() / 60.0)


# --- 2. near-miss tracking (#185, diagnostic only) ---------------------------
def _track_approach(trade: SimTrade, bar: B.Bar) -> None:
    """Record how close each resting order came to filling. Pure bookkeeping —
    it changes no decision, and exists so "the simulator under-fills" can be
    checked against a distribution instead of debated."""
    for leg in trade.legs:
        if leg.is_working:
            F.track_approach(leg, trade.direction, bar)


# --- 3. TTL ------------------------------------------------------------------
def _expire_working(trade: SimTrade, bar: B.Bar) -> None:
    """Retire resting orders past their TTL, via the shipped precedence
    (`staging.entry_expiry_reason`) for a staged trade so the absolute
    `max_entry_age_minutes` ceiling still wins over a freshly-reset leg clock."""
    entry_age = _minutes(trade.signal_at, bar.ts)
    for leg in trade.legs:
        if not leg.is_working or leg.placed_at is None:
            continue
        leg_age = _minutes(leg.placed_at, bar.ts)
        if trade.entry_style == STAGED and trade.staged_cfg:
            deployed = bool(leg.tranche) and leg.tranche != LAD.WHEN_SIGNAL
            reason = STG.entry_expiry_reason(
                trade.staged_cfg, leg_age_minutes=leg_age,
                entry_age_minutes=entry_age,
                entry_ttl_minutes=trade.entry_ttl_minutes, deployed=deployed)
        else:
            ttl = int(trade.entry_ttl_minutes or 0)
            reason = "leg_ttl" if (ttl > 0 and leg_age > ttl) else None
        if reason:
            # Counted BEFORE retiring it: expiry runs ahead of fills, so an
            # order whose TTL lapsed on a bar it would have filled in is lost
            # here. That is the cheapest of #185's candidate causes to
            # eliminate, and a count settles it without changing behaviour.
            if F.would_fill(leg, trade.direction, bar):
                leg.expired_on_fillable_bar = True
                trade.expired_on_fillable_bar += 1
            F.expire(leg, bar.ts, reason)


# --- 4. fills ----------------------------------------------------------------
def _fill_working(trade: SimTrade, bar: B.Bar, slip: float) -> None:
    for leg in trade.legs:
        if leg.is_working:
            F.try_fill(leg, trade.direction, bar, slippage_points=slip)


# --- 5. exits ----------------------------------------------------------------
def _resolve_exits(trade: SimTrade, bar: B.Bar, variant: Variant, slip: float) -> None:
    acct = variant.account(trade.account_id)
    fx = acct.fx_factor if acct else Decimal("1")
    for leg in trade.legs:
        if not leg.is_open:
            continue
        if F.resolve_exit(leg, trade.direction, bar, slippage_points=slip):
            F.settle(leg, trade.direction,
                     value_per_point=variant.instrument.value_per_point, fx_factor=fx)
            if leg.same_bar_ambiguous:
                trade.same_bar_ambiguous += 1
            if leg.outcome == F.TP_HIT:
                trade.closed_tp_hits.add(leg.tp_index)


# --- 6. MFE + ratchet ---------------------------------------------------------
def _ratchet(trade: SimTrade, bar: B.Bar, variant: Variant) -> None:
    """Update the max-favourable excursion and apply the SL rules.

    Hit detection runs off the MFE over the FULL TP ladder (#148/#149), unioned
    with the indices of legs that actually closed `tp_hit` — a level the market
    reached then retraced still counts, and a reclaim STOP leg that expired
    without ever closing must not drop its TP index out of the ladder.

    `current_price` for the non-TP triggers (`price_move`, `be_lock_at_r`) is the
    bar's FAVOURABLE extreme by default: live, a monitor polling many times a
    minute would have seen it. `ratchet_price: "close"` reproduces a once-a-bar
    poll instead. Whichever is chosen is recorded on the run — this is a stated
    modelling choice, not a hidden one."""
    if not trade.ever_filled:
        return
    fav = B.favourable_extreme(trade.direction, bar)
    if trade.mfe is None:
        trade.mfe = fav
    else:
        trade.mfe = max(trade.mfe, fav) if trade.direction == B.BUY \
            else min(trade.mfe, fav)

    open_legs = trade.open_legs
    if not open_legs or not trade.sl_rules:
        return
    tps_hit = levels_reached(trade.direction, Decimal(str(trade.mfe)), trade.tp_ladder)
    tps_hit |= trade.closed_tp_hits
    price = Decimal(str(fav if variant.ratchet_price == "extreme" else B.close_mid(bar)))
    for leg in open_legs:
        ctx = PositionCtx(
            side=trade.direction,
            entry=entry_basis(leg.fill_price, leg.entry),
            current_sl=Decimal(str(leg.sl)), current_price=price,
            tps=trade.tp_ladder, initial_sl=Decimal(str(leg.initial_sl)))
        new_sl = evaluate(ctx, trade.sl_rules, tps_hit)
        if new_sl is not None:
            leg.sl = float(new_sl)
            leg.sl_moved = True
            leg.note(bar.ts, "sl_moved")


# --- 7. cancel_pending_on_stop -----------------------------------------------
def _cancel_pending(trade: SimTrade, bar: B.Bar) -> None:
    """Retire still-resting orders once the trade is over or has progressed —
    the shipped `cancel_reason` decides which, including the `stopped_out` case
    an armed reclaim STOP would otherwise survive for another hour (#161)."""
    if not trade.cancel_pending_on_stop:
        return
    statuses = [F.CLOSED if l.status == F.CLOSED else l.status for l in trade.legs]
    reason = ST.cancel_reason(
        statuses,
        tps_hit=bool(trade.closed_tp_hits),
        sl_moved=any(l.sl_moved for l in trade.legs))
    if not reason:
        return
    for leg in trade.legs:
        if leg.is_working:
            F.cancel(leg, bar.ts, reason)
        elif leg.is_pending:
            _drop_pending(leg, bar.ts)
    for tr in trade.tranches:
        if tr.state not in STG.TERMINAL_STATES:
            tr.state, tr.reason = STG.CANCELLED, reason


def _dec(v, default: str = "0") -> Decimal:
    try:
        return Decimal(str(v))
    except (ArithmeticError, TypeError, ValueError):
        return Decimal(default)
