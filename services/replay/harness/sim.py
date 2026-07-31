"""The trade simulator: plan a signal with the REAL engines, then walk it bar by
bar (#169 §2).

FIDELITY IS THE WHOLE POINT. A harness that reimplements the logic tests a
different bot and is worse than useless, so every behavioural decision below is
delegated to the shipped function:

    execution/planner.build_plan          entry model, chase guard, TP geometry
    execution/staging.build_staged_legs   the tranche partition + deploy levels
    execution/staging.decide_tranche      the DECIDE engine (runner / reclaim)
    execution/staging.stop_too_tight      the staged -> single-shot fallback
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
    2. TTL expiry of still-resting orders
    3. entry fills
    4. exits — ADVERSE FIRST, same-bar TP+SL scored as the stop
    5. MFE update + SL ratchet, effective from the NEXT bar
    6. cancel_pending_on_stop

Step 5 lands after step 4 on purpose: a stop that a rule would have ratcheted
during this bar does not get to protect the position retroactively within it.
Live, the monitor observes and then acts; the same is true here.

PURE — stdlib + beacon_core. No DB, no clock, no broker.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

from beacon_core.execution import staging as STG
from beacon_core.execution import strategy as ST
from beacon_core.execution.planner import FanoutPlan, build_plan, validate_signal
from beacon_core.parsing.models import ParsedSignal
from beacon_core.risk.sizing import cap_total_risk, plan_total_risk, size_legs
from beacon_core.strategy.rules import (PositionCtx, entry_basis, evaluate,
                                        levels_reached)

from . import bars as B
from . import fills as F
from .context import MarketContext
from .variants import ResolvedConfig, Variant

# Roles whose legs are NOT placed at signal time — the monitor deploys them when
# the DECIDE engine says so.
DEFERRED_ROLES = (STG.RUNNER, STG.RECLAIM)

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
    if want_staged:
        staged_cfg = STG.staged_config(ep.get("staged"))
        near, deep = STG.zone_edges(signal.direction, signal.entry_from, signal.entry_to)
        atr = mc.atr_1h
        sl_dist = abs(float(deep) - float(signal.sl))
        # The SAME fallback the executor takes (#156): no 1h ATR, or a stop too
        # tight to stage a break around, and the account runs single-shot. Without
        # it a replayed "staged" arm would silently include trades live ran flat.
        if atr is None or STG.stop_too_tight(sl_dist, atr, staged_cfg):
            want_staged = False
        else:
            market_hint = honor_hint and (signal.order_type_hint or "").upper() == "MARKET"
            legs = STG.build_staged_legs(
                direction=signal.direction, tps=signal.tps, near_edge=near,
                deep_edge=deep, sl=signal.sl, atr=atr, current_price=mc.current_price,
                cfg=staged_cfg, min_stop_distance=variant.min_stop_distance,
                market_hint=market_hint, chase_tolerance_r=chase_r,
                chase_tolerance_atr=chase_atr, beyond_tolerance=beyond,
                max_tp_distance_pct=max_tp_pct if max_tp_pct > 0 else None,
                candle_high=mc.candle_high, candle_low=mc.candle_low)
            if not legs:
                # beyond_tolerance="skip" declined the whole signal (#155).
                return None, "chase_guard_skip"
            plan = FanoutPlan(symbol=signal.symbol, direction=signal.direction,
                              order_type="LIMIT", legs=legs)
            entry_style = STAGED
            staged_geo = {"near": float(near), "deep": float(deep), "atr": float(atr)}
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
        deferred = l.tranche in DEFERRED_ROLES
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
    """Group the planned legs by tranche role, preserving `staging.ROLES` order so
    the DECIDE loop is deterministic regardless of TP-ladder depth."""
    by_role: Dict[str, Tranche] = {}
    for i, leg in enumerate(legs):
        role = leg.tranche
        if role is None:
            continue
        t = by_role.get(role)
        if t is None:
            t = Tranche(role=role, state_since=at)
            # A toe-in is placed by the executor at signal time, so it is already
            # deployed when the trade starts existing.
            t.state = STG.DEPLOYED if role == STG.TOE_IN else STG.PENDING
            by_role[role] = t
        t.leg_indices.append(i)
    return [by_role[r] for r in STG.ROLES if r in by_role]


# ============================ the bar step ====================================
def step(trade: SimTrade, bar: B.Bar, *, variant: Variant) -> None:
    """Advance `trade` through one bar. Idempotent for a finished trade."""
    if trade.is_done:
        return
    slip = variant.slippage_points
    if trade.entry_style == STAGED:
        _staged_step(trade, bar)
    _expire_working(trade, bar)
    _fill_working(trade, bar, slip)
    _resolve_exits(trade, bar, variant, slip)
    _ratchet(trade, bar, variant)
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
    """Run the shipped DECIDE engine for every unresolved tranche.

    The monitor feeds it a live snapshot each tick; here the snapshot is the bar.
    Both price inputs are the bar's ADVERSE EXTREME, not its close:

      * `max_adverse_beyond_deep` — a break beyond the deep edge that retraced
        inside the minute is still a break, and reading the close would
        systematically under-arm the reclaim.
      * `price`, which `_mechanical_decision` uses to ask whether price has
        reached the deep edge. A monitor polling every few seconds sees the
        extreme; reading the close would under-deploy the runner on exactly the
        wicks a deep-edge LIMIT is placed to catch, so the replayed staged arm
        would diverge from the live one it is meant to stand in for."""
    geo, cfg = trade.staged_geo, trade.staged_cfg
    if not geo or not cfg:
        return
    adverse_px = B.adverse_extreme(trade.direction, bar)
    trade.max_adverse_beyond_deep = max(
        trade.max_adverse_beyond_deep,
        STG.beyond_deep(trade.direction, adverse_px, geo["deep"]))
    ctx = STG.StagingContext(
        direction=trade.direction, near_edge=geo["near"], deep_edge=geo["deep"],
        sl=float(trade.initial_sl), price=adverse_px, atr=geo.get("atr"),
        max_adverse_beyond_deep=trade.max_adverse_beyond_deep)
    for tr in trade.tranches:
        if tr.role == STG.TOE_IN or tr.state in STG.TERMINAL_STATES:
            continue
        mins = _minutes(tr.state_since, bar.ts)
        d = STG.decide_tranche(role=tr.role, state=tr.state, ctx=ctx, cfg=cfg,
                               minutes_in_state=mins)
        tr.reason = d.reason
        if d.action == STG.DEPLOY:
            _deploy(trade, tr, d, bar)
        elif d.action == STG.EXPIRE:
            _resolve_tranche(trade, tr, STG.EXPIRED, bar.ts)
        elif d.action == STG.SKIP:
            _resolve_tranche(trade, tr, STG.SKIPPED, bar.ts)


def _deploy(trade: SimTrade, tr: Tranche, decision, bar: B.Bar) -> None:
    """Place a tranche's orders. A reclaim goes to ARMED (a STOP resting at the
    broker); a runner goes to DEPLOYED. Each leg's TTL clock starts now (#158)."""
    level = decision.level if decision.level is not None else trade.staged_geo["deep"]
    for i in tr.leg_indices:
        leg = trade.legs[i]
        if not leg.is_pending:
            continue
        leg.deploy(bar.ts, decision.mode or "LIMIT", float(level),
                   trigger=float(level) if decision.mode == STG.MODE_STOP else None)
    tr.mode = decision.mode
    tr.trigger_level = float(level)
    tr.state = STG.ARMED if decision.mode == STG.MODE_STOP else STG.DEPLOYED
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


# --- 2. TTL ------------------------------------------------------------------
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
            deployed = leg.tranche in DEFERRED_ROLES
            reason = STG.entry_expiry_reason(
                trade.staged_cfg, leg_age_minutes=leg_age,
                entry_age_minutes=entry_age,
                entry_ttl_minutes=trade.entry_ttl_minutes, deployed=deployed)
        else:
            ttl = int(trade.entry_ttl_minutes or 0)
            reason = "leg_ttl" if (ttl > 0 and leg_age > ttl) else None
        if reason:
            F.expire(leg, bar.ts, reason)


# --- 3. fills ----------------------------------------------------------------
def _fill_working(trade: SimTrade, bar: B.Bar, slip: float) -> None:
    for leg in trade.legs:
        if leg.is_working:
            F.try_fill(leg, trade.direction, bar, slippage_points=slip)


# --- 4. exits ----------------------------------------------------------------
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


# --- 5. MFE + ratchet ---------------------------------------------------------
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


# --- 6. cancel_pending_on_stop -----------------------------------------------
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
