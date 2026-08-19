"""Monitor service — the capital-preservation loop.

Every tick it: reconciles legs against live broker positions/orders, detects
TP/SL closes, applies each source's SL-move rules off the LIVE price (so a
reversal is acted on without waiting for a fill confirmation), and cancels
stale unfilled limit entries past their TTL.

Note (Phase 1): exact fill/close prices for LIMIT legs are correlated
heuristically over REST. SL-move decisions do NOT depend on that correlation —
they run off live price — so capital protection is unaffected. A later phase
should read /history for exact P&L attribution.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import or_, select

from beacon_core.ai import service as ai_service
from beacon_core.bus import Bus
from beacon_core.config import CH_TRADE_EVENT, get_settings, effective_entry_ttl_min
from beacon_core.logging import get_logger
from beacon_core.health import run_health_server
from beacon_core.db.base import Session, init_models
from beacon_core.db.models import (Account, Broker, Event, ExecutionStrategy, Leg,
                                   PositionActivity, Signal, Source, Trade,
                                   StagedEntry, StagedTranche)
from beacon_core.execution import attribution as ATTR
from beacon_core.execution import strategy as ST
from beacon_core.execution import staging as STG
from beacon_core.brokers import build_adapter, symbol_map
from beacon_core.brokers.types import (AuthError, ModifyPositionRequest, OrderSide,
                                       OrderStatus, OrderType, PlaceOrderRequest)
from beacon_core.risk.sizing import deployed_risk_from_planned
from beacon_core.strategy.rules import (PositionCtx, entry_basis, evaluate,
                                        levels_reached, next_mfe, DEFAULT_SL_RULES)
from beacon_core.settings_store import get_setting, set_setting
from beacon_core.analysis import broker_truth as BT
from beacon_core.analysis import epochs as EP
from beacon_core.analysis import structure as S
from beacon_core.analysis import structure_map as struct_map
from beacon_core.tasks import spawn_bg
from beacon_core.timeutil import utcnow, parse_iso_utc
from beacon_core import notifications as notify
from beacon_core.notifications import digest as DG
from beacon_core.notifications.context import build_ctx
from beacon_core.notifications.throttle import Throttle, suffix as _burst_suffix

log = get_logger("monitor")
settings = get_settings()
bus = Bus()

OPEN_LEG = ("pending", "working", "open")


def _notify(event_id: str, ctx: dict) -> None:
    """Fire-and-forget a notification (own DB session). Best-effort — never
    lets a notification failure affect position monitoring."""
    async def _run():
        try:
            async with Session()() as s:
                await notify.notify(s, event_id, ctx)
        except Exception as exc:                 # pragma: no cover - defensive
            log.debug("notify %s failed: %s", event_id, exc)
    spawn_bg(_run())


# A dropped session fails every trade on every tick until it is restored, so an
# un-debounced broker_error would arrive once per trade per monitor interval.
# Collapse a burst into one alert that reports how many it stood for (#180).
_BROKER_ERR = Throttle()
_BROKER_ERR_DETAIL_MAX = 300


def _broker_error(kind: str, detail: str, *, symbol=None, account_id=None) -> None:
    """Debounced `broker_error` notification, keyed by failure kind + account +
    symbol so one storm collapses without masking a different failure."""
    ok, suppressed = _BROKER_ERR.allow(f"{kind}:{account_id}:{symbol}")
    if not ok:
        return
    _notify("broker_error", {
        "symbol": symbol,
        "detail": _burst_suffix(str(detail)[:_BROKER_ERR_DETAIL_MAX], suppressed)})


async def _closed_ctx(session, trade) -> dict:
    """The trade_closed notification ctx, assembled by the SAME build_ctx the
    test-fire uses — so the live message and a test render identically (joins
    channel + account + open/close times + P&L). Best-effort; never raises here
    because the caller fires it fire-and-forget."""
    try:
        return await build_ctx(session, "trade_closed", trade=trade)
    except Exception as exc:                     # pragma: no cover - defensive
        log.debug("trade_closed ctx build failed (trade %s): %s", trade.id, exc)
        return {"symbol": trade.symbol, "direction": trade.direction,
                "pl": str(trade.realized_pl) if trade.realized_pl is not None else None}

# |realized P&L| at or below this (in the ACCOUNT currency) counts as a
# break-even close. The primary BE signal is the SL being ratcheted to ~entry
# and then hit; this catches a residual-near-zero close too.
BE_MONEY_TOL = Decimal("0.05")


def _is_close_txn(t: dict) -> bool:
    """A broker transaction that represents a position CLOSE (realized P&L),
    not an open/deposit/fee. Capital.com marks closes with note 'Trade closed'."""
    note = (t.get("note") or "").lower()
    if note:
        return "clos" in note
    return (t.get("type") or "") in ("TRADE", "DEAL", "POSITION")


def _outcome_from_source(src) -> str | None:
    """Map a Capital.com activity `source` to a leg outcome. None = unknown, so
    the caller falls back to the price/P&L heuristic."""
    s = (src or "").upper()
    if s == "SL":
        return "sl_hit"
    if s in ("TP", "PROFIT", "TAKE_PROFIT", "LIMIT"):
        return "tp_hit"
    if s == "USER":
        return "manual"
    return None


async def _open_trades(session):
    return (await session.execute(
        select(Trade).where(Trade.status.in_(("open", "partial"))))).scalars().all()


async def _rules_for(session, trade) -> tuple[list, dict, int, "Decimal | None"]:
    sig = await session.get(Signal, trade.signal_id)
    source = await session.get(Source, sig.source_id) if sig and sig.source_id else None
    strat = dict((source.strategy if source else {}) or {})
    # The signal's original stop — the IMMUTABLE R denominator for be_lock_at_r
    # (#109). We use the signal SL rather than leg.sl because leg.sl is mutated as
    # the stop trails; None -> the trigger fails open.
    initial_sl = Decimal(str(sig.sl)) if sig and sig.sl is not None else None

    # Resolve the ExecutionStrategy for this trade's (account, source) scope — only
    # the ≤4 rows whose scope could match (#84).
    chain = []
    if sig and sig.source_id is not None:
        rows = (await session.execute(select(ExecutionStrategy).where(
            or_(ExecutionStrategy.account_id.is_(None),
                ExecutionStrategy.account_id == trade.account_id),
            or_(ExecutionStrategy.source_id.is_(None),
                ExecutionStrategy.source_id == sig.source_id)))).scalars().all()
        chain = ST.resolve_chain(rows, trade.account_id, sig.source_id)

    # cancel_pending_on_stop is part of the exit pillar (live-resolved; a boolean op
    # flag, no need to freeze it): cascades most-specific -> (Any,Any) -> default.
    strat["cancel_pending_on_stop"] = ST.cancel_pending_on_stop(
        chain, source_strategy=strat, default=True)

    # Manage each trade by the exit-rules SNAPSHOT stamped at entry — this is what
    # makes the per-account A/B point-in-time (arm frozen at entry, immune to later
    # strategy edits). Pre-snapshot trades resolve live: strategy.exit_policy ->
    # source -> global default.
    if getattr(trade, "sl_rules", None):
        return trade.sl_rules, strat, effective_entry_ttl_min(strat), initial_sl
    gcfg = await get_setting(session, "strategy", {}) or {}
    rules, _origin = ST.exit_sl_rules(chain, source_rules=strat.get("sl_rules"),
                                      global_default=gcfg.get("default_sl_rules"))
    return rules, strat, effective_entry_ttl_min(strat), initial_sl


# Persistent broker sessions, reused across ticks. Re-logging in (and switching
# account) on every 5s tick floods Capital.com's /session rate limit and stalls
# reconciliation. We keep one logged-in adapter per account and only rebuild it
# on an auth failure.
_ADAPTERS: dict = {}


async def _adapter_for(session, account_id: int):
    acct = await session.get(Account, account_id)
    broker = await session.get(Broker, acct.broker_id)
    adapter = _ADAPTERS.get(account_id)
    if adapter is None:                                # reconcile the mapped account
        _, adapter = await build_adapter(session, acct)
        _ADAPTERS[account_id] = adapter
    return acct, broker, adapter


async def _evict_adapter(account_id: int) -> None:
    adapter = _ADAPTERS.pop(account_id, None)
    if adapter is not None:
        try:
            await adapter.aclose()
        except Exception:
            pass


# High/low of the IN-PROGRESS bar per (broker, epic), fetched at most ONCE per tick
# (cleared in `tick()`). Two things fall out of that: one bar fetch serves every
# trade on the instrument, and every arm of the same signal folds the IDENTICAL
# wick into its MFE, so they can no longer disagree about which TPs were reached —
# bars are public market data, so sharing one across accounts is what makes the
# arms consistent by construction rather than by luck of poll timing (#160).
_CANDLE: dict = {}
# The finest resolution Capital.com offers — the tighter the bar, the less
# pre-entry price action an in-progress candle can carry.
_MFE_CANDLE_RESOLUTION = "MINUTE"


async def _candle_extremes(adapter, broker_id, epic) -> tuple:
    """(high, low) of the in-progress bar for `epic` as MID prices, or (None, None).
    Best-effort and cached per tick: if the fetch fails the MFE simply falls back to
    point quotes (pre-#160 behaviour) — it must never disturb position management."""
    key = (broker_id, epic)
    cached = _CANDLE.get(key)
    if cached is not None:
        return cached
    hi = lo = None
    try:
        bars = await adapter.get_bars(epic, _MFE_CANDLE_RESOLUTION, max_bars=2)
        if bars:
            last = bars[-1]
            hi = Decimal(str(last["h"])) if last.get("h") is not None else None
            lo = Decimal(str(last["l"])) if last.get("l") is not None else None
    except Exception as exc:
        log.info("candle fetch failed for %s: %s", epic, exc)
    _CANDLE[key] = (hi, lo)
    return hi, lo


def _classify_outcome(close_px, entry_px, tp, sl, sl_moved, tol, realized_pl=None) -> str:
    """Name a close: tp_hit | sl_hit | breakeven | manual.

    Break-even is detected by EITHER condition (per product decision):
      * the stop was ratcheted to ~entry and then hit, or
      * the realized P&L is ~0 (|pl| <= BE_MONEY_TOL).
    Otherwise we use the close level's proximity to the TP/SL, and finally the
    sign of realized P&L as a tie-breaker (a position auto-closes at its
    attached profitLevel/stopLevel, so profit => TP, loss => SL)."""
    # --- break-even (either condition) ---
    if sl_moved and close_px is not None and abs(close_px - entry_px) <= tol:
        return "breakeven"
    if realized_pl is not None and abs(realized_pl) <= BE_MONEY_TOL:
        return "breakeven"
    # --- by close level proximity ---
    if close_px is not None:
        near_tp = abs(close_px - tp) <= tol
        near_sl = abs(close_px - sl) <= tol
        if near_tp and not near_sl:
            return "tp_hit"
        if near_sl and not near_tp:
            return "sl_hit"
    # --- fall back to realized P&L sign ---
    if realized_pl is not None:
        if realized_pl > 0:
            return "tp_hit"
        if realized_pl < 0:
            return "sl_hit"
    return "manual"


async def _analyze_outcome(session, trade, ai_cfg) -> None:
    """Best-effort AI post-mortem when a trade has just fully closed."""
    if ai_cfg is None or not (ai_cfg.ready and ai_cfg.analyze_outcomes):
        return
    try:
        legs = (await session.execute(select(Leg).where(Leg.trade_id == trade.id))).scalars().all()
        sig = await session.get(Signal, trade.signal_id)
        source = await session.get(Source, sig.source_id) if sig and sig.source_id else None
        trade_dict = {
            "symbol": trade.symbol, "direction": trade.direction,
            "planned_risk": str(trade.planned_risk) if trade.planned_risk else None,
            "realized_pl": str(trade.realized_pl),
            "source_name": source.name if source else "unknown",
            "legs": [{"tp_index": l.tp_index, "outcome": l.outcome,
                      "entry": str(l.entry),
                      "close_price": str(l.close_price) if l.close_price is not None else None,
                      "realized_pl": str(l.realized_pl) if l.realized_pl is not None else None}
                     for l in legs],
        }
        await ai_service.assess_outcome(session, trade_dict, trade.id, cfg=ai_cfg)
    except Exception as exc:                          # never break the loop
        log.warning("AI outcome analysis failed (trade %s): %s", trade.id, exc)


async def _staged_pending(session, trade_id) -> list:
    """Non-terminal tranches (pending) for a staged trade — the ones the DECIDE
    engine still acts on. Once deployed/armed, a tranche's legs are ordinary
    working orders and the normal reconciliation/TTL machinery owns them."""
    return (await session.execute(select(StagedTranche).where(
        StagedTranche.trade_id == trade_id,
        StagedTranche.state == "pending"))).scalars().all()


async def _drive_staged(session, trade, adapter, smap, mid, ttl_min) -> None:
    """Drive the confirmation-staged entry engine (#129) for one trade, one tick:
    update the max adverse excursion, then for each PENDING tranche run the DECIDE
    engine and carry out the decision. DEPLOY places the tranche's frozen legs
    (runner LIMIT at the deep edge, reclaim STOP at the reclaim trigger) and flips
    them to 'working' with a fresh TTL clock — from there the existing monitor
    machinery reconciles fills, ratchets stops, and TTL-cancels, so no new
    fill/close code is introduced. EXPIRE abandons the never-deployed legs. This
    is the monitor's ONLY order-placing path; it runs only for staged trades."""
    se = (await session.execute(select(StagedEntry).where(
        StagedEntry.trade_id == trade.id))).scalar_one_or_none()
    if se is None:
        return
    pending = await _staged_pending(session, trade.id)
    if not pending:
        return

    direction = se.direction
    deep = float(se.deep_edge)
    cfg = STG.staged_config(se.cfg)
    # Running max adverse excursion beyond the deep edge (what arms the reclaim).
    excursion = STG.beyond_deep(direction, float(mid), deep)
    if excursion > float(se.max_adverse_beyond_deep or 0):
        se.max_adverse_beyond_deep = Decimal(str(round(excursion, 6)))
    ctx = STG.StagingContext(
        direction=direction, near_edge=float(se.near_edge), deep_edge=deep,
        sl=float(se.sl), price=float(mid),
        atr=float(se.atr) if se.atr is not None else None,
        max_adverse_beyond_deep=float(se.max_adverse_beyond_deep or 0))
    now = utcnow()

    for tr in pending:
        minutes = (now - tr.state_since).total_seconds() / 60.0
        decision = STG.decide_tranche(role=tr.role, state=tr.state, ctx=ctx, cfg=cfg,
                                      minutes_in_state=minutes)
        if decision.action == STG.WAIT:
            continue
        legs = (await session.execute(select(Leg).where(
            Leg.id.in_(tr.leg_ids or []), Leg.status == "staged"))).scalars().all()
        if decision.action in (STG.EXPIRE, STG.SKIP):
            for leg in legs:                       # never placed at the broker -> just mark
                leg.status = "expired"
                leg.outcome = "expired"
                leg.closed_at = now
            tr.state = "expired" if decision.action == STG.EXPIRE else "skipped"
            tr.state_since = now
            tr.reason = (decision.reason or "")[:96]
            session.add(Event(trade_id=trade.id, kind="staged_" + tr.state,
                              payload={"tranche": tr.role, "reason": decision.reason}))
            log.info("trade %s tranche %s -> %s (%s)", trade.id, tr.role, tr.state, decision.reason)
            continue
        if decision.action != STG.DEPLOY:
            continue
        # DEPLOY: place the frozen legs as LIMIT (runner) or STOP (reclaim).
        # The resting window is deliberate, not inherited (#158): deployed_ttl_minutes
        # when configured, else the resolved entry TTL — i.e. today's behaviour.
        # Still clamped to the global [MIN, MAX] entry-TTL bounds like any other TTL
        # (ttl_min is already clamped by _rules_for, so this only bounds the override).
        good_till = utcnow() + timedelta(
            minutes=effective_entry_ttl_min(
                {"entry_ttl_minutes": STG.deployed_ttl_minutes(cfg, ttl_min)}))
        side_buy = direction == "BUY"
        # #140: the runner deploys MARKET once price has reached the deep edge (a
        # LIMIT resting there would be a limit-at-market and get rejected). STOP is
        # the reclaim re-entry; everything else is a resting LIMIT.
        is_market = decision.mode == "MARKET"
        order_type = (OrderType.STOP if decision.mode == "STOP"
                      else OrderType.MARKET if is_market else OrderType.LIMIT)
        placed = 0
        for leg in legs:
            try:
                res = await adapter.place_order(PlaceOrderRequest(
                    broker_symbol=smap.broker_epic,
                    side=OrderSide.BUY if side_buy else OrderSide.SELL,
                    order_type=order_type,
                    quantity=leg.lot,
                    # MARKET ignores the level; LIMIT/STOP need it (deep edge / reclaim trigger).
                    limit_price=None if is_market else leg.entry,
                    stop_loss=leg.sl, take_profit=leg.tp,
                    good_till=None if is_market else good_till))
                if res.status == OrderStatus.REJECTED:
                    leg.status, leg.outcome = "rejected", "rejected"
                    session.add(Event(trade_id=trade.id, leg_id=leg.id, kind="reject",
                                      payload={"ref": res.broker_order_ref,
                                               "reason": res.rejection_reason}))
                else:
                    if is_market:
                        # A MARKET runner opens a position immediately — link it by
                        # broker_position_ref (like the executor's market toe-in) so
                        # reconciliation matches the position, not a working order
                        # that never exists.
                        leg.broker_position_ref = res.broker_order_ref
                        leg.status = "open" if res.status == OrderStatus.FILLED else "pending"
                        # Never persist a 0 fill — leave it NULL so the entry/R
                        # basis falls back to leg.entry and the ratchet still
                        # works; the next tick backfills it from the position (#159).
                        leg.fill_price = res.fill_price or None
                        if leg.fill_price is None and res.status == OrderStatus.FILLED:
                            session.add(Event(trade_id=trade.id, leg_id=leg.id,
                                              kind="fill_price_unknown",
                                              payload={"ref": res.broker_order_ref,
                                                       "tranche": tr.role}))
                    else:
                        leg.broker_order_ref = res.broker_order_ref
                        leg.status = "working"
                    leg.created_at = utcnow()      # reset the TTL clock: deployed/armed late
                    tr.broker_order_ref = res.broker_order_ref
                    placed += 1
                    session.add(Event(trade_id=trade.id, leg_id=leg.id, kind="staged_deployed",
                                      payload={"tranche": tr.role, "mode": decision.mode,
                                               "reason": decision.reason}))
            except Exception as exc:               # one leg failing must not sink the tick
                leg.status = "rejected"
                session.add(Event(trade_id=trade.id, leg_id=leg.id, kind="reject",
                                  payload={"error": str(exc)[:300]}))
                log.warning("staged deploy failed (trade %s leg %s): %s", trade.id, leg.id, exc)
                _broker_error("staged_deploy", f"Staged entry deploy failed: {exc}",
                              symbol=trade.symbol, account_id=trade.account_id)
            await asyncio.sleep(1.0 / max(settings.broker_rate_per_sec, 0.1))
        tr.state = "armed" if decision.mode == "STOP" else "deployed"
        tr.state_since = utcnow()
        tr.mode = decision.mode
        if decision.level is not None:
            tr.trigger_level = Decimal(str(decision.level))
        tr.reason = (decision.reason or "")[:96]
        log.info("trade %s tranche %s -> %s (%s legs, %s)", trade.id, tr.role, tr.state,
                 placed, decision.reason)


async def _record_deployed_risk(session, trade) -> None:
    """Persist what the fills ACTUALLY put on, to the ORIGINAL stop (#188).

    Recomputed from every leg each tick rather than accumulated incrementally:
    idempotent, so a restart, a re-tick or a late fill all converge on the same
    number instead of double-counting. Cheap — the legs are already loaded by
    the caller's own query path.

    The original stop comes from `signals.sl`, NOT `legs.sl`: the ratchet engine
    mutates the leg stop in place, so a trade that moved to breakeven would
    otherwise record ~0 deployed risk and flatter the very ratio this exists to
    measure.

    Measurement only — nothing reads this on the trading path."""
    sig = await session.get(Signal, trade.signal_id)
    if sig is None or sig.sl is None:
        return
    legs = (await session.execute(select(Leg).where(
        Leg.trade_id == trade.id))).scalars().all()
    planned = [(l.lot, l.entry, sig.sl) for l in legs]
    filled = [(l.lot, l.fill_price, sig.sl) for l in legs
              if l.fill_price is not None]
    try:
        dep = deployed_risk_from_planned(planned_risk=trade.planned_risk,
                                         planned_legs=planned, filled_legs=filled)
    except (ArithmeticError, TypeError, ValueError):
        return                       # a measurement must never break the monitor
    if dep is not None:
        trade.deployed_risk = dep


async def _process_trade(session, trade, ai_cfg=None) -> None:
    # Measurement first: it must be recorded even on the tick that closes the
    # trade, and the close path below returns early.
    try:
        await _record_deployed_risk(session, trade)
    except Exception as exc:                     # never let telemetry stop the loop
        log.debug("deployed_risk skipped for trade %s: %s", trade.id, exc)
    legs = (await session.execute(select(Leg).where(
        Leg.trade_id == trade.id, Leg.status.in_(OPEN_LEG)))).scalars().all()
    # A staged trade stays alive while any tranche is still pending — even if its
    # toe-in already filled and closed — so the runner/reclaim can still deploy (#129).
    staged_pending = await _staged_pending(session, trade.id)
    if not legs and not staged_pending:
        if trade.status != "closed":
            trade.status = "closed"
            _notify("trade_closed", await _closed_ctx(session, trade))
            await _analyze_outcome(session, trade, ai_cfg)
        return

    acct, broker, adapter = await _adapter_for(session, trade.account_id)
    try:
        smap = await symbol_map(session, broker.id, trade.symbol)
        if not smap:
            return
        quote = await adapter.get_quote(smap.broker_epic)
        price = (quote.bid if trade.direction == "BUY" else quote.offer) or quote.last_price
        if price is None:
            return
        # Cache the freshest live price so the structure-map on-break check (#115)
        # can gate on it without an extra broker call (mid, side-agnostic).
        mid = (quote.bid + quote.offer) / 2 if (quote.bid is not None and quote.offer is not None) else price
        _LAST_PRICE[trade.symbol] = (float(mid), utcnow())

        positions = {p.broker_position_ref: p for p in await adapter.list_positions()}
        orders = {o.broker_order_ref: o for o in await adapter.list_orders()}
        # Index live positions by the working order they were created from — the
        # reliable key to link a filled working order to the exact leg that
        # placed it (Capital.com's position.workingOrderId == our order dealId).
        pos_by_wo = {p.working_order_ref: p for p in positions.values() if p.working_order_ref}
        rules, strat, ttl_min, initial_sl = await _rules_for(session, trade)
        # Staged TTL context (#158). A tranche deployed by the monitor restarts its
        # legs' TTL clock, so those legs answer to `deployed_ttl_minutes` rather
        # than the signal-time entry TTL, and the whole entry answers to the
        # absolute `max_entry_age_minutes` ceiling. Loaded once per tick, only for
        # staged trades. Pre-#156 trades have entry_style NULL: they fall back to
        # the old behaviour once their tranches settle, which is what they did
        # before — and both knobs default to it anyway.
        staged_ttl_cfg, late_leg_ids, tranches = None, set(), []
        if trade.entry_style == "staged" or staged_pending:
            _se = (await session.execute(select(StagedEntry).where(
                StagedEntry.trade_id == trade.id))).scalar_one_or_none()
            if _se is not None:
                staged_ttl_cfg = STG.staged_config(_se.cfg)
                # ALL tranches, not just the non-toe-in ones: besides the TTL clock
                # we now have to advance a tranche's state when its legs resolve, so
                # `armed` with a live broker_order_ref can't outlive the order (#161).
                tranches = (await session.execute(select(StagedTranche).where(
                    StagedTranche.trade_id == trade.id))).scalars().all()
                # toe-in is placed by the executor at signal time and keeps the
                # ordinary TTL; only the monitor-deployed tranches reset the clock.
                for _tr in tranches:
                    if _tr.role != STG.TOE_IN:
                        late_leg_ids.update(_tr.leg_ids or [])

        def _tranche_of(leg_id):
            for _tr in tranches:
                if leg_id in (_tr.leg_ids or []):
                    return _tr
            return None

        def _resolve_tranche(leg_id, state: str, reason: str) -> None:
            """Advance the tranche that owns `leg_id` to a TERMINAL state once its
            leg resolves (#161). Without this a reclaim stays `armed` forever with a
            broker_order_ref that is long gone, so 'is an order still resting?' is
            unanswerable from the DB — 15 CLOSED trades read that way today."""
            tr = _tranche_of(leg_id)
            if tr is None or tr.state in STG.TERMINAL_STATES:
                return
            tr.state = state
            tr.state_since = utcnow()
            tr.reason = (reason or "")[:96]
            session.add(Event(trade_id=trade.id, leg_id=leg_id, kind="staged_" + state,
                              payload={"tranche": tr.role, "reason": reason}))
        # TP price map across ALL legs of the trade, so (a) a ratchet rule that
        # references a TP whose legs already closed still resolves its level, and
        # (b) hit-DETECTION runs off the FULL ladder — a TP consumed as a staged
        # reclaim STOP that expired still counts as reached once price trades
        # through it, so the runner ratchets to BE like the single-shot arms
        # (#148). Detecting hits off the open legs only silently dropped it.
        _all_legs = (await session.execute(select(Leg).where(
            Leg.trade_id == trade.id))).scalars().all()
        tp_levels = {l.tp_index: Decimal(str(l.tp)) for l in _all_legs}
        # Persist the max-favorable excursion and detect hits off IT, not just the
        # instantaneous price (#149). A staged runner's intervening TPs are reclaim
        # STOPs that expire without closing `tp_hit`, so nothing latches a level the
        # market reached then retraced between two polls — the single-shot LIMIT arms
        # fill on the spike at broker granularity, the runner only sees our ticks.
        # Tracking the best price (max for BUY, min for SELL) keeps a reached TP
        # latched for the open runner. NULL MFE (pre-#149 trades / DB without the
        # ALTER, CLAUDE.md §6) falls back to live price → behaviour unchanged.
        # ...and fold the in-progress candle's extreme in too, so a TP wicked
        # BETWEEN two polls is not lost and every arm of the signal sees the same
        # wick whenever it happened to poll (#160). Bars are MID prices while
        # `price` is the side we would close on (bid for BUY, offer for SELL), so
        # step the extreme back by half the spread — an MFE must never claim a
        # level the TRADEABLE side never reached, or a stop ratchets to breakeven
        # on a high that was only ever a mid.
        _c_hi, _c_lo = await _candle_extremes(adapter, broker.id, smap.broker_epic)
        _half_spread = ((quote.offer - quote.bid) / 2
                        if (quote.bid is not None and quote.offer is not None
                            and quote.offer > quote.bid) else Decimal("0"))
        prev_mfe = trade.max_favorable_price
        mfe = next_mfe(trade.direction, prev_mfe, price,
                       candle_high=None if _c_hi is None else _c_hi - _half_spread,
                       candle_low=None if _c_lo is None else _c_lo + _half_spread)
        if prev_mfe is None or Decimal(str(prev_mfe)) != mfe:
            trade.max_favorable_price = mfe
        tps_hit = levels_reached(trade.direction, mfe, tp_levels)

        # Positions already linked to an open leg of ANY trade (not just this one)
        # so the epic+direction heuristic can never re-claim a broker position
        # that another leg already owns. A per-trade set let two same-symbol/
        # -direction trades each grab the SAME dealId, putting one
        # broker_position_ref on legs across trades and double-counting its
        # realized P&L (#9). Global uniqueness is the invariant: 1 position -> 1 leg.
        _claimed = (await session.execute(select(Leg.broker_position_ref).where(
            Leg.status == "open", Leg.broker_position_ref.isnot(None)))).scalars().all()
        linked_refs = {r for r in _claimed if r}

        def _find_fill_heuristic():
            """Fallback ONLY: an open position on this epic+direction not yet
            linked. Used when the broker gave us no workingOrderId to match on.
            Ambiguous when a signal fans out into several same-direction legs, so
            it runs strictly AFTER the exact workingOrderId match."""
            for ref, p in positions.items():
                if ref in linked_refs:
                    continue
                if (p.broker_symbol == smap.broker_epic
                        and str(p.direction).endswith(trade.direction)):
                    return p
            return None

        vpp = Decimal(str(smap.value_per_point))

        # Broker deal history: fetched lazily (only when a close is detected) and
        # once per tick. Each closing row carries the position dealId and the
        # realized P&L (account currency) — the source of truth. A close is
        # matched to its leg by dealId (exact); a working order that filled AND
        # closed inside one tick (never linked to a position) falls back to an
        # unclaimed close on the same instrument.
        _txn_cache = {"loaded": False, "list": []}

        async def _transactions():
            if not _txn_cache["loaded"]:
                _txn_cache["loaded"] = True
                getter = getattr(adapter, "get_transactions", None)
                if getter is not None:
                    try:
                        _txn_cache["list"] = await getter(last_period=6 * 3600)
                    except Exception as exc:
                        log.warning("history fetch failed (trade %s): %s", trade.id, exc)
            return _txn_cache["list"]

        def _txn_by_dealid(txns, deal_id):
            """The unclaimed closing transaction for this exact position dealId."""
            if not deal_id:
                return None
            for t in txns:
                if t.get("_used") or not _is_close_txn(t):
                    continue
                if str(t.get("deal_id") or "") == str(deal_id):
                    t["_used"] = True
                    return t
            return None

        def _txn_by_instrument(txns, epic):
            """Best-effort: an unclaimed close on this instrument, for a working
            order that filled and closed before we ever saw its position id."""
            e = (epic or "").upper()
            for t in txns:
                if t.get("_used") or not _is_close_txn(t):
                    continue
                inst = (t.get("instrument") or "").upper()
                if inst and (inst == e or inst in e or e in inst):
                    t["_used"] = True
                    return t
            return None

        def _txn_lookup(txns, deal_id):
            """Read-only close lookup by dealId (does NOT consume the row)."""
            if not deal_id:
                return None
            for t in txns:
                if not _is_close_txn(t):
                    continue
                if str(t.get("deal_id") or "") == str(deal_id):
                    return t
            return None

        # Broker activity log: the authoritative "why" behind each state change
        # (fill / SL / TP / user edit / close). Fetched lazily, once per tick.
        _act_cache = {"loaded": False, "list": []}

        async def _activities():
            if not _act_cache["loaded"]:
                _act_cache["loaded"] = True
                getter = getattr(adapter, "get_activity", None)
                if getter is not None:
                    try:
                        _act_cache["list"] = await getter(last_period=6 * 3600)
                    except Exception as exc:
                        log.warning("activity fetch failed (trade %s): %s", trade.id, exc)
            return _act_cache["list"]

        def _close_source(acts, position_ref):
            """The activity `source` (SL/TP/USER) that CLOSED this position — the
            newest POSITION activity for the deal that isn't the SYSTEM open."""
            if not position_ref:
                return None
            for a in acts:                       # get_activity returns newest-first
                if str(a.get("deal_id") or "") != str(position_ref):
                    continue
                if (a.get("type") or "").upper() != "POSITION":
                    continue
                if (a.get("source") or "").upper() == "SYSTEM":
                    continue
                return a.get("source")
            return None

        async def _close_leg(leg, txns, require_txn: bool = False,
                             exact_only: bool = False) -> bool:
            """Close a leg. Realized P&L and the close identity come from the
            broker's closing transaction, matched to THIS leg by position dealId
            (source of truth). With require_txn=True the leg is only closed when
            such a transaction exists — used to tell a genuine fill+close apart
            from a cancelled/expired working order.

            exact_only=True drops the instrument-level fallback, for a leg the
            broker still lists as a RESTING order: it cannot have filled, so any
            unclaimed close on the instrument belongs to some OTHER leg. Letting it
            match there labelled a never-filled reclaim STOP `closed`/`sl_hit` with
            `fill_price = None` — and, because the close 'succeeded', we skipped the
            cancel and left the order live at the broker (#161, trade t391)."""
            tp = Decimal(str(leg.tp)); sl = Decimal(str(leg.sl))
            entry_px = entry_basis(leg.fill_price, leg.entry)   # 0 == unknown (#159)
            tol = Decimal(str(smap.min_stop_distance or "0")) or (abs(tp) * Decimal("0.001"))
            lot = Decimal(str(leg.lot))

            m = _txn_by_dealid(txns, leg.broker_position_ref)
            if m is None and not leg.broker_position_ref and not exact_only:
                m = _txn_by_instrument(txns, smap.broker_epic)
            if m is None and require_txn:
                return False

            # WHETHER the leg closed and WHICH transaction settled it are two
            # questions, and only the second one needs the dealId (#234). An
            # instrument-level match answers the first -- the position is gone
            # from the broker -- and nothing at all about the second, so its
            # money is not read here. Reading it is what put another position's
            # amount on 506 legs, to the cent, across lots differing by 3.6x.
            basis = ATTR.classify(m, leg.broker_position_ref)
            realized_pl = m.get("pl") if basis == ATTR.ATTR_EXACT else None

            # The transaction has no close level on this API, so derive a close
            # price for the ledger from the live price vs the leg's levels.
            tp_reached = (price >= tp) if trade.direction == "BUY" else (price <= tp)
            sl_reached = (price <= sl) if trade.direction == "BUY" else (price >= sl)
            if tp_reached or abs(price - tp) <= tol:
                close_px = tp
            elif sl_reached or abs(price - sl) <= tol:
                close_px = sl
            else:
                close_px = price

            # Outcome: the broker's own reason (activity source) is the truth;
            # fall back to the price/P&L heuristic only when it's unknown. A stop
            # that had been ratcheted to ~entry is a break-even, not a loss.
            src_outcome = _outcome_from_source(
                _close_source(await _activities(), leg.broker_position_ref))
            if src_outcome == "sl_hit" and bool(leg.sl_moved) and abs(close_px - entry_px) <= tol:
                outcome = "breakeven"
            elif src_outcome is not None:
                outcome = src_outcome
            else:
                outcome = _classify_outcome(close_px, entry_px, tp, sl,
                                            bool(leg.sl_moved), tol, realized_pl)

            if basis == ATTR.ATTR_EXACT:
                src = "broker"
                # `size` can arrive unsigned; a stop-out is always a loss. The
                # rule lives in `analysis/broker_truth` so the activity audit
                # below applies the SAME one — it did not, and the two ledgers
                # drifted apart by twice the ratcheted losses (#202).
                realized_pl = BT.signed_close_pl(realized_pl, outcome)
            elif basis == ATTR.ATTR_HEURISTIC:
                # No broker row at all — price-derived P&L in INSTRUMENT
                # currency, stamped as the estimate it is rather than passing
                # for a settled amount.
                dist = (close_px - entry_px)
                if trade.direction == "SELL":
                    dist = -dist
                realized_pl = dist * lot * vpp
                src = "heuristic"
            else:
                # A transaction exists but it is not this leg's. Both ways of
                # filling the gap are the bug: copy the foreign amount (what
                # produced the 47,524.5), or substitute the price estimate,
                # which would dress an unknown as a measurement and read as
                # settled money in every rollup downstream.
                realized_pl = None
                src = ATTR.ATTR_UNATTRIBUTED

            # Was this close attributed by the EXACT position dealId (source of
            # truth), or only heuristically? Surface the latter so a mis-matched
            # leg P&L is auditable instead of silently trusted (#9).
            _exact = basis == ATTR.ATTR_EXACT
            leg.outcome = outcome
            leg.close_price = close_px
            leg.realized_pl = realized_pl
            leg.pl_attribution = basis
            leg.status = "closed"
            leg.closed_at = utcnow()
            # The leg's position ref is NOT taken from the matched transaction.
            # It only ever could be when the match was heuristic (an exact match
            # needs the ref to have existed already), and adopting a foreign
            # dealId is what made the defect permanent: the leg then carried
            # another position's identity, so every later query saw a
            # well-referenced leg and nothing could find the error again short
            # of comparing lot sizes (#234, #9 step 4).
            session.add(Event(trade_id=trade.id, leg_id=leg.id, kind="closed",
                              payload={"outcome": outcome, "source": src, "basis": basis,
                                       "pl": None if realized_pl is None else str(realized_pl)}))
            if not _exact:
                session.add(Event(trade_id=trade.id, leg_id=leg.id,
                                  kind="reconcile_unmatched",
                                  payload={"reason": "no exact dealId close txn",
                                           "attributed_via": src, "basis": basis,
                                           "pl": None if realized_pl is None else str(realized_pl),
                                           "candidate_deal_id": str((m or {}).get("deal_id") or ""),
                                           "position_ref": str(leg.broker_position_ref or "")}))
            await bus.publish(CH_TRADE_EVENT, {"trade_id": trade.id, "leg_id": leg.id,
                                               "outcome": outcome})
            _ev = {"tp_hit": "tp_hit", "sl_hit": "sl_hit", "breakeven": "sl_hit"}.get(outcome)
            if _ev:
                _notify(_ev, {"symbol": trade.symbol, "direction": trade.direction,
                              "size": str(leg.lot) if leg.lot is not None else None,
                              "price": str(close_px) if close_px is not None else None,
                              "pl": None if realized_pl is None else str(realized_pl),
                              "detail": f"TP{leg.tp_index} — {outcome}"})
            return True

        async def _audit_activities():
            """Persist every broker activity for THIS trade's deals into the
            PositionActivity audit table (idempotently), attaching realized P&L +
            currency to a close. This is the queryable 'truth' log for analysis."""
            acts = await _activities()
            if not acts:
                return
            all_trade_legs = (await session.execute(select(Leg).where(
                Leg.trade_id == trade.id))).scalars().all()
            leg_by_deal = {}
            for l in all_trade_legs:
                if l.broker_order_ref:
                    leg_by_deal[str(l.broker_order_ref)] = l
                if l.broker_position_ref:
                    leg_by_deal[str(l.broker_position_ref)] = l
            if not leg_by_deal:
                return
            txns = await _transactions()
            seen: set = set()
            for a in acts:
                did = str(a.get("deal_id") or "")
                leg = leg_by_deal.get(did)
                if leg is None:
                    continue
                at = parse_iso_utc(a.get("date"))
                atype = a.get("type")
                key = (did, at, atype)
                if key in seen:
                    continue
                seen.add(key)
                exists = (await session.execute(select(PositionActivity.id).where(
                    PositionActivity.account_id == trade.account_id,
                    PositionActivity.deal_id == did,
                    PositionActivity.activity_at == at,
                    PositionActivity.type == atype))).first()
                if exists:
                    continue
                rp = cur = None
                if (atype or "").upper() == "POSITION" and (a.get("source") or "").upper() != "SYSTEM":
                    t = _txn_lookup(txns, did)
                    if t is not None:
                        rp, cur = t.get("pl"), t.get("currency")
                        # #202: the transaction amount arrives UNSIGNED when the
                        # stop was modified before the close, and this write path
                        # used to store it raw while the leg path repaired it —
                        # so a ratcheted stop-out was logged here as a GAIN. That
                        # single omission is the whole "phantom tax": the two
                        # ledgers differed by exactly twice the ratcheted losses.
                        # This runs after pass 1, so `leg.outcome` is already the
                        # broker's own close reason.
                        rp = BT.signed_close_pl(rp, leg.outcome)
                session.add(PositionActivity(
                    account_id=trade.account_id, trade_id=trade.id, leg_id=leg.id,
                    epic=a.get("epic"), deal_id=did, deal_reference=a.get("deal_reference"),
                    source=a.get("source"), type=atype, status=a.get("status"),
                    realized_pl=rp, currency=cur, activity_at=at, payload=a.get("raw") or {}))

        # --- pass 1: reconcile fills, detect + attribute closes ---
        for leg in legs:
            if leg.status == "working":
                # Still resting on the book -> only TTL can act on it.
                if leg.broker_order_ref in orders:
                    # A monitor-deployed tranche rests for `deployed_ttl_minutes`
                    # measured from ITS deploy; everything else uses the signal-time
                    # entry TTL. The absolute ceiling is measured from the signal
                    # and overrides both (#158).
                    age = (utcnow() - leg.created_at).total_seconds() / 60.0
                    if staged_ttl_cfg is not None:
                        _why = STG.entry_expiry_reason(
                            staged_ttl_cfg, leg_age_minutes=age,
                            entry_age_minutes=(utcnow() - trade.created_at
                                               ).total_seconds() / 60.0,
                            entry_ttl_minutes=ttl_min,
                            deployed=leg.id in late_leg_ids)
                    else:
                        _why = ("leg_ttl" if (ttl_min and ttl_min > 0 and age > ttl_min)
                                else None)
                    if _why:
                        # Never expire something that actually filled+closed. The
                        # broker still lists this order as resting, so only an EXACT
                        # dealId close can mean that — see _close_leg (#161).
                        if not await _close_leg(leg, await _transactions(),
                                                require_txn=True, exact_only=True):
                            try:
                                await adapter.cancel_order(leg.broker_order_ref)
                            except Exception:
                                pass
                            leg.status = "expired"
                            leg.outcome = "expired"
                            leg.closed_at = utcnow()
                            session.add(Event(trade_id=trade.id, leg_id=leg.id,
                                              kind="expired",
                                              payload={"age_min": age, "reason": _why}))
                            _resolve_tranche(leg.id, STG.EXPIRED, _why)
                        else:
                            _resolve_tranche(leg.id, STG.FILLED, "filled and closed")
                    continue

                # Left the order book: it became a position (fill), filled+closed
                # inside a tick, or was cancelled. Prefer the exact workingOrderId
                # link; fall back to the epic+direction heuristic.
                pos = pos_by_wo.get(leg.broker_order_ref) or _find_fill_heuristic()
                if pos is not None:
                    leg.broker_position_ref = pos.broker_position_ref
                    leg.status = "open"
                    if pos.avg_open_price is not None and not leg.fill_price:
                        leg.fill_price = pos.avg_open_price     # also repairs a stored 0 (#159)
                    linked_refs.add(pos.broker_position_ref)
                    via = "workingOrderId" if pos.working_order_ref == leg.broker_order_ref else "heuristic"
                    session.add(Event(trade_id=trade.id, leg_id=leg.id, kind="filled",
                                      payload={"position": pos.broker_position_ref, "via": via}))
                    _notify("order_filled", {
                        "symbol": trade.symbol, "direction": trade.direction,
                        "size": str(leg.lot) if leg.lot is not None else None,
                        "price": str(leg.fill_price) if leg.fill_price is not None else None,
                        "detail": f"TP{leg.tp_index} entry filled"})
                    _resolve_tranche(leg.id, STG.FILLED, f"filled via {via}")
                elif not await _close_leg(leg, await _transactions(), require_txn=True):
                    leg.status = "cancelled"
                    leg.outcome = "cancelled"
                    leg.closed_at = utcnow()
                    session.add(Event(trade_id=trade.id, leg_id=leg.id,
                                      kind="cancelled_at_broker", payload={}))
                    _resolve_tranche(leg.id, STG.CANCELLED, "gone from the order book")
                else:
                    _resolve_tranche(leg.id, STG.FILLED, "filled and closed in one tick")
                continue

            if (leg.status == "open" and leg.broker_position_ref
                    and leg.broker_position_ref not in positions):
                await _close_leg(leg, await _transactions(), require_txn=False)

        # --- which TPs have actually been reached (persisted across ticks)? ---
        closed_all = (await session.execute(select(Leg).where(
            Leg.trade_id == trade.id, Leg.status == "closed"))).scalars().all()
        achieved = {l.tp_index for l in closed_all if l.outcome == "tp_hit"}
        effective_tps_hit = tps_hit | achieved

        # --- pass 2: SL-move ratchet on still-open legs ---
        for leg in legs:
            if leg.status != "open" or not leg.broker_position_ref:
                continue
            if leg.broker_position_ref not in positions:
                continue
            # Self-heal an unknown fill: the broker's open level is authoritative
            # and we re-read it every tick anyway. Covers a MARKET leg whose
            # confirm came back without a level, and legs written before #159.
            _pos = positions[leg.broker_position_ref]
            if not leg.fill_price and _pos.avg_open_price is not None:
                leg.fill_price = _pos.avg_open_price
                session.add(Event(trade_id=trade.id, leg_id=leg.id,
                                  kind="fill_price_backfilled",
                                  payload={"fill_price": str(_pos.avg_open_price),
                                           "position": leg.broker_position_ref}))
            ctx = PositionCtx(
                side=trade.direction,
                entry=entry_basis(leg.fill_price, leg.entry),   # 0 == unknown (#159)
                current_sl=Decimal(str(leg.sl)), current_price=price, tps=tp_levels,
                initial_sl=initial_sl)          # immutable original stop -> R for be_lock_at_r (#109)
            new_sl = evaluate(ctx, rules, effective_tps_hit)
            if new_sl is not None:
                try:
                    await adapter.modify_position(ModifyPositionRequest(
                        broker_position_ref=leg.broker_position_ref, stop_loss=new_sl))
                    leg.sl = new_sl
                    leg.sl_moved = True
                    session.add(Event(trade_id=trade.id, leg_id=leg.id,
                                      kind="sl_moved", payload={"new_sl": str(new_sl)}))
                    _notify("sl_moved", {"symbol": trade.symbol, "direction": trade.direction,
                                         "sl": str(new_sl), "detail": f"TP{leg.tp_index} stop moved"})
                    log.info("trade %s leg %s SL -> %s", trade.id, leg.id, new_sl)
                except Exception as exc:
                    log.warning("SL move failed (leg %s): %s", leg.id, exc)
                    # The stop the ratchet decided on is NOT at the broker — the
                    # position is still exposed at the old level. Push it.
                    _broker_error("sl_move", f"Stop move to {new_sl} rejected: {exc}",
                                  symbol=trade.symbol, account_id=trade.account_id)

        # --- retire resting entries: the trade progressed, or it is fully out ---
        # `progressed`: a TP was hit or a stop ratcheted, so the remaining unfilled
        # LIMIT entries of THIS trade are stale — price moved the intended way
        # without them (e.g. a 4025-4020 buy where 4025 filled and hit TP1 while
        # 4020 never triggered). Cancel them so we don't enter late.
        # `stopped_out`: EVERY position of the trade has closed. Anything still
        # resting — a ladder rung, or a staged reclaim STOP sitting ARMED — would
        # open a fresh position on a trade that is over, with no ratchet, no exit
        # rules and no risk accounting. Only the leg TTL retired those before, up to
        # an hour after the trade died (#161). Both are gated by the SAME per-source
        # `cancel_pending_on_stop` flag, which is finally true to its name.
        # Only cancels orders the broker confirms are still cancellable, so an order
        # that just filled is never mislabelled — and never fires before a leg of
        # THIS trade actually filled (#25).
        why_cancel = ST.cancel_reason([l.status for l in _all_legs],
                                      tps_hit=bool(effective_tps_hit),
                                      sl_moved=any(l.sl_moved for l in _all_legs))
        if why_cancel and strat.get("cancel_pending_on_stop", True):
            _detail = {ST.CANCEL_PROGRESSED: "trade progressed (TP hit / SL ratcheted); stale entry",
                       ST.CANCEL_STOPPED_OUT: "trade fully closed; orphaned resting entry"}[why_cancel]
            for leg in _all_legs:
                if leg.status != "working" or leg.broker_order_ref not in orders:
                    continue
                ok = False
                try:
                    ok = await adapter.cancel_order(leg.broker_order_ref)
                except Exception as exc:
                    log.warning("rule-cancel failed (leg %s): %s", leg.id, exc)
                if ok:
                    leg.status = "cancelled"
                    leg.outcome = "cancelled"
                    leg.closed_at = utcnow()
                    session.add(Event(trade_id=trade.id, leg_id=leg.id, kind="cancelled_by_rule",
                                      payload={"reason": _detail, "trigger": why_cancel}))
                    _resolve_tranche(leg.id, STG.CANCELLED, _detail)
                    _notify("order_cancelled", {
                        "symbol": trade.symbol, "direction": trade.direction,
                        "detail": f"TP{leg.tp_index} entry cancelled — {why_cancel}"})
                    log.info("trade %s leg %s cancelled — %s", trade.id, leg.id, why_cancel)
            # A dead trade must not deploy anything NEW either: retire the tranches
            # that were never placed, so `_drive_staged` below can't put a fresh
            # order on the book the same tick we cleared the old one.
            if why_cancel == ST.CANCEL_STOPPED_OUT:
                for tr in await _staged_pending(session, trade.id):
                    for leg in (await session.execute(select(Leg).where(
                            Leg.id.in_(tr.leg_ids or []),
                            Leg.status == "staged"))).scalars().all():
                        leg.status = "cancelled"
                        leg.outcome = "cancelled"
                        leg.closed_at = utcnow()
                    tr.state = STG.CANCELLED
                    tr.state_since = utcnow()
                    tr.reason = _detail[:96]
                    session.add(Event(trade_id=trade.id, kind="staged_cancelled",
                                      payload={"tranche": tr.role, "reason": _detail}))
                    log.info("trade %s tranche %s -> cancelled (stopped out)", trade.id, tr.role)
                staged_pending = []

        # --- confirmation-staged entry (#129): drive the DECIDE engine off live
        # price. Runs only for staged trades; deploys the runner at the deep edge
        # and arms the reclaim STOP on a break — its deployed legs then flow through
        # the same reconciliation/ratchet/TTL above on later ticks. Best-effort:
        # a staging failure must never disturb capital management of open legs.
        if staged_pending:                          # skip the StagedEntry query for normal trades
            try:
                await _drive_staged(session, trade, adapter, smap, mid, ttl_min)
            except Exception as exc:
                log.warning("staged-entry drive failed (trade %s): %s", trade.id, exc)

        # Persist the broker activity audit for this trade (best-effort).
        try:
            await _audit_activities()
        except Exception as exc:
            log.warning("activity audit failed (trade %s): %s", trade.id, exc)

        # roll up trade
        remaining = (await session.execute(select(Leg).where(
            Leg.trade_id == trade.id, Leg.status.in_(OPEN_LEG)))).scalars().all()
        still_staged = await _staged_pending(session, trade.id)
        closed = (await session.execute(select(Leg).where(
            Leg.trade_id == trade.id, Leg.status == "closed"))).scalars().all()
        trade.realized_pl = sum((Decimal(str(l.realized_pl)) for l in closed
                                 if l.realized_pl is not None), Decimal("0"))
        if not remaining and not still_staged:     # #129: don't close while a tranche is pending
            was_closed = trade.status == "closed"
            trade.status = "closed"
            if not was_closed:
                _notify("trade_closed", await _closed_ctx(session, trade))
                await _analyze_outcome(session, trade, ai_cfg)
        elif closed:
            trade.status = "partial"
    except AuthError:
        # Session went bad — drop it so the next tick logs in fresh.
        await _evict_adapter(trade.account_id)
        raise


async def tick() -> None:
    _CANDLE.clear()             # one bar fetch per epic per tick, shared by all arms
    async with Session()() as session:
        ai_cfg = await ai_service.load_config(session)
        trades = await _open_trades(session)
        for trade in trades:
            try:
                await _process_trade(session, trade, ai_cfg)
            except Exception as exc:
                log.warning("trade %s tick failed: %s", trade.id, exc)
        await session.commit()


_STRUCT_RECOMPUTE_KEY = "structure_last_recompute"

# Freshest live mid-price per symbol, captured by the trade loop, so the on-break
# structure check (#115) never needs its own broker call. {symbol: (price, ts)}.
_LAST_PRICE: dict = {}
_PRICE_TTL_S = 300          # ignore a cached price older than this for the break check


async def _structure_break(s, cfg, last, now):
    """On-break gate for the map recompute (#115): return a short reason string
    when the live price has broken beyond the ACTIVE map's dealing range on
    `break_trigger_tf` by more than `break_atr_buffer` * ATR (fib anchors stale),
    else None. Debounced against the last recompute so one break can't re-fire
    repeatedly. Uses the trade loop's cached price (no extra broker call); if none
    is fresh, the daily scheduled cadence still covers the map."""
    debounce = float(cfg.get("break_debounce_minutes", 30)) * 60
    if last is not None and (now - last).total_seconds() < debounce:
        return None
    tf = str(cfg.get("break_trigger_tf", "4h"))
    buffer_atr = float(cfg.get("break_atr_buffer", 0.25))
    for symbol in (cfg.get("symbols") or []):
        cached = _LAST_PRICE.get(symbol)
        if cached is None or (now - cached[1]).total_seconds() > _PRICE_TTL_S:
            continue
        m = await struct_map.active_map(s, symbol)
        if not m:
            continue
        st = m["structures"].get(tf)
        if st is None:
            continue
        brk = S.range_break(cached[0], st.range_low, st.range_high, st.atr, buffer_atr)
        if brk:
            return f"{symbol}:{brk}"
    return None


async def _maybe_recompute_structure() -> None:
    """Recompute the persistent structure/magnet map (#61) when either gate fires (#115):

      * SCHEDULED — a new UTC day has started (config `recompute_cadence_days`,
        default 1), anchored to the day boundary so it fires once per day at a
        consistent point instead of drifting by run time; or
      * ON BREAK — the live price has broken beyond the active map's dealing range
        on `break_trigger_tf`, invalidating the fib anchors (debounced).

    Best-effort, background, own session — zero impact on the trade loop. The
    timestamp is stamped BEFORE launching so a slow recompute never re-triggers."""
    try:
        async with Session()() as s:
            cfg = await struct_map.load_config(s)
            if not cfg.get("enabled"):
                return
            now = utcnow()
            last = parse_iso_utc(await get_setting(s, _STRUCT_RECOMPUTE_KEY, None))
            reason = None
            if S.scheduled_recompute_due(last, now, cfg.get("recompute_cadence_days", 1)):
                reason = "scheduled"
            else:
                brk = await _structure_break(s, cfg, last, now)
                if brk:
                    reason = f"break:{brk}"
            if reason is None:
                return
            await set_setting(s, _STRUCT_RECOMPUTE_KEY, now.isoformat())
            await s.commit()
        log.info("structure: recompute due (%s) — launching in background", reason)
        spawn_bg(struct_map.recompute_all(cfg))
    except Exception as exc:                    # never disturb the monitor loop
        log.warning("structure recompute check failed: %s", exc)


_DARK_ARM_KEY = "arm_dark_state"        # {account_id: last-alert ISO}
_DARK_ARM_CFG_KEY = "arm_dark"          # operator overrides, all optional


async def _decisions_since(session, since) -> dict:
    """Per account: (signals decided, signals skipped by filtration) since `since`.

    Deduped by SIGNAL on both sides. One signal fans out to several legs and each
    can log its own `entry_filtered`, so counting events would inflate the skip
    side against a trade side that is naturally one-per-signal, and manufacture a
    dark arm out of a busy one."""
    out: dict = {}

    def _slot(acct):
        return out.setdefault(int(acct), {"decided": set(), "skipped": set()})

    rows = (await session.execute(
        select(Event.payload).where(Event.kind == "entry_filtered",
                                    Event.ts >= since))).scalars().all()
    for p in rows:
        if not isinstance(p, dict) or p.get("reason") != "filtration_skip":
            continue                       # de-size / desize events are not skips
        acct, sig = p.get("account_id"), p.get("signal_id")
        if acct is None or sig is None:
            continue
        s = _slot(acct)
        s["skipped"].add(sig)
        s["decided"].add(sig)
    taken = (await session.execute(
        select(Trade.account_id, Trade.signal_id)
        .where(Trade.created_at >= since, Trade.signal_id.isnot(None)))).all()
    for acct, sig in taken:
        if acct is not None:
            _slot(acct)["decided"].add(sig)
    return {a: (len(v["decided"]), len(v["skipped"])) for a, v in out.items()}


async def _check_dark_arms() -> None:
    """Alarm when an A/B arm has stopped trading (#200).

    An arm skipping ~100% of its signals produces no trades and no information —
    it is a broken experiment, not a conservative one. Arm B took 1 trade on
    2026-08-05 and 0 on 08-06 while the control took 35 and 28, and the only
    thing that noticed was a human looking at a flat account two days later; the
    hand-tightening that followed orphaned a 52-removal accumulation.

    READ-ONLY over the ledger and completely off the trading path: it writes an
    `events` row and fires a notification, and touches no position. Debounced per
    account so a genuinely dark arm alerts once per cooldown rather than once per
    monitor tick."""
    try:
        async with Session()() as s:
            cfg = await get_setting(s, _DARK_ARM_CFG_KEY, {}) or {}
            if cfg.get("enabled") is False:
                return
            window_h = float(cfg.get("window_hours", 24))
            cooldown_h = float(cfg.get("cooldown_hours", 6))
            min_signals = int(cfg.get("min_signals", EP.DARK_MIN_SIGNALS))
            threshold = float(cfg.get("threshold", EP.DARK_SKIP_RATE))

            now = utcnow()
            since = now - timedelta(hours=window_h)
            counts = await _decisions_since(s, since)
            if not counts:
                return
            state = dict(await get_setting(s, _DARK_ARM_KEY, {}) or {})
            fired = False
            for account_id, (n_decided, n_skipped) in sorted(counts.items()):
                verdict = EP.dark_arm(n_decided, n_skipped,
                                      min_signals=min_signals, threshold=threshold)
                if not verdict["dark"]:
                    state.pop(str(account_id), None)   # recovered — re-arm the alarm
                    continue
                last = parse_iso_utc(state.get(str(account_id)))
                if last is not None and (now - last).total_seconds() < cooldown_h * 3600:
                    continue
                account = await s.get(Account, account_id)
                detail = (f"{verdict['reason']} over the last {window_h:g}h. An arm "
                          f"that is not trading is not a treatment — check its "
                          f"entry_filters before its epoch is read as evidence.")
                s.add(Event(kind="arm_dark", payload={
                    "account_id": account_id, "window_hours": window_h, **verdict}))
                _notify("arm_dark", {"account": account.name if account else
                                     f"account {account_id}", "detail": detail})
                log.warning("arm_dark: account %s — %s", account_id, verdict["reason"])
                state[str(account_id)] = now.isoformat()
                fired = True
            if fired or state != (await get_setting(s, _DARK_ARM_KEY, {}) or {}):
                await set_setting(s, _DARK_ARM_KEY, state)
            await s.commit()
    except Exception as exc:                    # never disturb the monitor loop
        log.warning("dark-arm check failed: %s", exc)


_DIGEST_KEY = "daily_summary_last"      # the DATE last reported, not when
_DIGEST_CFG_KEY = "daily_summary"       # {enabled, at_utc}


async def _maybe_send_daily_digest() -> None:
    """Send yesterday's operator digest once, shortly after the day ends (#198).

    The guard stores the DATE the last digest covered, not the time it was sent.
    That is what makes "exactly one per day" survive a restart: a monitor that
    comes back at 15:00 sends yesterday's digest once and does not then walk
    backwards filling in the week. A late digest is useful; a burst of stale
    ones trains the operator to ignore the channel.

    Read-only over the ledger, own session, and wrapped so a reporting failure
    can never disturb the position loop."""
    try:
        async with Session()() as s:
            cfg = await get_setting(s, _DIGEST_CFG_KEY, {}) or {}
            if cfg.get("enabled") is False:
                return
            day = DG.digest_due(await get_setting(s, _DIGEST_KEY, None),
                                utcnow(), cfg.get("at_utc", DG.DEFAULT_AT_UTC))
            if day is None:
                return
            rows = await DG.build_digests(s, day)
            # Stamp BEFORE sending, and stamp even with nothing to report: a
            # send failure must not queue a duplicate on the next tick, and a
            # quiet day is still a day that has been reported on.
            await set_setting(s, _DIGEST_KEY, day)
            await s.commit()
        for ctx in rows:
            _notify("daily_summary", ctx)
        log.info("daily digest for %s: %s account(s)", day, len(rows))
    except Exception as exc:                    # never disturb the monitor loop
        log.warning("daily digest failed: %s", exc)


async def _sweep_orphaned_orders() -> None:
    """Start-up safety net (#161): cancel any order still resting at the broker
    whose trade we already consider CLOSED.

    The tick loop only ever looks at open/partial trades, so an order left behind
    by a crash (or by a restart between deciding to cancel and committing) is
    invisible to it and rests until its TTL — long enough to fill and open an
    unmanaged position. We only ever touch orders WE placed (looked up by our own
    `broker_order_ref`) and only when the broker still lists them as cancellable.
    Best-effort: a broker error here must never stop the monitor from starting."""
    try:
        async with Session()() as session:
            rows = (await session.execute(
                select(Leg, Trade).join(Trade, Leg.trade_id == Trade.id).where(
                    Leg.status == "working", Leg.broker_order_ref.isnot(None),
                    Trade.status == "closed"))).all()
            if not rows:
                return
            by_account: dict = {}
            for leg, trade in rows:
                by_account.setdefault(trade.account_id, []).append(leg)
            for account_id, legs in by_account.items():
                try:
                    _, _, adapter = await _adapter_for(session, account_id)
                    live = {o.broker_order_ref for o in await adapter.list_orders()}
                except Exception as exc:
                    log.warning("orphan sweep: broker unreachable (account %s): %s",
                                account_id, exc)
                    _broker_error("unreachable", f"Broker unreachable: {exc}",
                                  account_id=account_id)
                    continue
                for leg in legs:
                    if leg.broker_order_ref not in live:
                        continue                    # already gone — nothing to pull
                    try:
                        ok = await adapter.cancel_order(leg.broker_order_ref)
                    except Exception as exc:
                        log.warning("orphan sweep: cancel failed (leg %s): %s", leg.id, exc)
                        continue
                    if ok:
                        leg.status = "cancelled"
                        leg.outcome = "cancelled"
                        leg.closed_at = utcnow()
                        session.add(Event(trade_id=leg.trade_id, leg_id=leg.id,
                                          kind="cancelled_by_rule",
                                          payload={"reason": "orphaned order on a closed trade",
                                                   "trigger": "startup_sweep"}))
                        log.warning("orphan sweep: cancelled resting order on closed "
                                    "trade %s (leg %s)", leg.trade_id, leg.id)
            await session.commit()
    except Exception as exc:                        # never block start-up
        log.warning("orphan sweep failed: %s", exc)


async def main() -> None:
    await init_models()
    await _sweep_orphaned_orders()
    spawn_bg(run_health_server("monitor", bus, port=8080))
    log.info("monitor loop every %ss", settings.monitor_interval)
    while True:
        try:
            await tick()
            await _maybe_recompute_structure()
            await _check_dark_arms()
            await _maybe_send_daily_digest()
        except Exception as exc:
            log.exception("tick failed: %s", exc)
        await asyncio.sleep(settings.monitor_interval)


if __name__ == "__main__":
    asyncio.run(main())
