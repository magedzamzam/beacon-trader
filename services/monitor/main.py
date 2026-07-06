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
import datetime as dt
from decimal import Decimal

from sqlalchemy import select

from beacon_core.ai import service as ai_service
from beacon_core.bus import Bus
from beacon_core.config import CH_TRADE_EVENT, get_settings
from beacon_core.logging import get_logger
from beacon_core.health import run_health_server
from beacon_core.db.base import Session, init_models
from beacon_core.db.models import (Account, Broker, Event, Leg, PositionActivity,
                                   Signal, Source, SymbolMap, Trade)
from beacon_core.brokers import get_adapter, resolve_credentials
from beacon_core.brokers.types import AuthError, ModifyPositionRequest
from beacon_core.strategy.rules import PositionCtx, evaluate
from beacon_core import notifications as notify

log = get_logger("monitor")
settings = get_settings()
bus = Bus()
_BG_TASKS: set = set()

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
    t = asyncio.create_task(_run())
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)

# |realized P&L| at or below this (in the ACCOUNT currency) counts as a
# break-even close. The primary BE signal is the SL being ratcheted to ~entry
# and then hit; this catches a residual-near-zero close too.
BE_MONEY_TOL = Decimal("0.05")


def _utcnow():
    return dt.datetime.now(dt.timezone.utc)


def _is_close_txn(t: dict) -> bool:
    """A broker transaction that represents a position CLOSE (realized P&L),
    not an open/deposit/fee. Capital.com marks closes with note 'Trade closed'."""
    note = (t.get("note") or "").lower()
    if note:
        return "clos" in note
    return (t.get("type") or "") in ("TRADE", "DEAL", "POSITION")


def _parse_dt(v):
    """Parse a broker ISO timestamp into a tz-aware UTC datetime (or None)."""
    if not v:
        return None
    try:
        d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except (ValueError, AttributeError):
        return None


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


async def _rules_for(session, trade) -> tuple[list, dict, int]:
    sig = await session.get(Signal, trade.signal_id)
    source = await session.get(Source, sig.source_id) if sig and sig.source_id else None
    strat = (source.strategy if source else {}) or {}
    return strat.get("sl_rules", []), strat, strat.get("entry_ttl_minutes", 60)


# Persistent broker sessions, reused across ticks. Re-logging in (and switching
# account) on every 5s tick floods Capital.com's /session rate limit and stalls
# reconciliation. We keep one logged-in adapter per account and only rebuild it
# on an auth failure.
_ADAPTERS: dict = {}


async def _adapter_for(session, account_id: int):
    acct = await session.get(Account, account_id)
    broker = await session.get(Broker, acct.broker_id)
    adapter = _ADAPTERS.get(account_id)
    if adapter is None:
        creds = resolve_credentials(broker.credentials_ref)
        creds.setdefault("is_demo", broker.is_demo)
        creds["account_id"] = acct.broker_account_id   # reconcile the mapped account
        adapter = get_adapter(broker.type, creds)
        _ADAPTERS[account_id] = adapter
    return acct, broker, adapter


async def _evict_adapter(account_id: int) -> None:
    adapter = _ADAPTERS.pop(account_id, None)
    if adapter is not None:
        try:
            await adapter.aclose()
        except Exception:
            pass


async def _symbol_map(session, broker_id, symbol):
    return (await session.execute(select(SymbolMap).where(
        SymbolMap.broker_id == broker_id,
        SymbolMap.internal_symbol == symbol))).scalar_one_or_none()


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


def _tps_hit(direction: str, price: Decimal, legs) -> set[int]:
    hit = set()
    for l in legs:
        tp = Decimal(str(l.tp))
        if direction == "BUY" and price >= tp:
            hit.add(l.tp_index)
        elif direction == "SELL" and price <= tp:
            hit.add(l.tp_index)
    return hit


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


async def _process_trade(session, trade, ai_cfg=None) -> None:
    legs = (await session.execute(select(Leg).where(
        Leg.trade_id == trade.id, Leg.status.in_(OPEN_LEG)))).scalars().all()
    if not legs:
        if trade.status != "closed":
            trade.status = "closed"
            _notify("trade_closed", {"symbol": trade.symbol, "direction": trade.direction,
                                     "pl": str(trade.realized_pl) if trade.realized_pl is not None else None})
            await _analyze_outcome(session, trade, ai_cfg)
        return

    acct, broker, adapter = await _adapter_for(session, trade.account_id)
    try:
        smap = await _symbol_map(session, broker.id, trade.symbol)
        if not smap:
            return
        quote = await adapter.get_quote(smap.broker_epic)
        price = (quote.bid if trade.direction == "BUY" else quote.offer) or quote.last_price
        if price is None:
            return

        positions = {p.broker_position_ref: p for p in await adapter.list_positions()}
        orders = {o.broker_order_ref: o for o in await adapter.list_orders()}
        # Index live positions by the working order they were created from — the
        # reliable key to link a filled working order to the exact leg that
        # placed it (Capital.com's position.workingOrderId == our order dealId).
        pos_by_wo = {p.working_order_ref: p for p in positions.values() if p.working_order_ref}
        rules, strat, ttl_min = await _rules_for(session, trade)
        tps_hit = _tps_hit(trade.direction, price, legs)
        # TP price map across ALL legs of the trade, so a ratchet rule that
        # references a TP whose legs already closed still resolves its level.
        _all_legs = (await session.execute(select(Leg).where(
            Leg.trade_id == trade.id))).scalars().all()
        tp_levels = {l.tp_index: Decimal(str(l.tp)) for l in _all_legs}

        # positions already linked to an open leg of this trade (so the heuristic
        # fallback never re-claims a position that is already tracked).
        linked_refs = {l.broker_position_ref for l in legs
                       if l.status == "open" and l.broker_position_ref}

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

        async def _close_leg(leg, txns, require_txn: bool = False) -> bool:
            """Close a leg. Realized P&L and the close identity come from the
            broker's closing transaction, matched to THIS leg by position dealId
            (source of truth). With require_txn=True the leg is only closed when
            such a transaction exists — used to tell a genuine fill+close apart
            from a cancelled/expired working order."""
            tp = Decimal(str(leg.tp)); sl = Decimal(str(leg.sl))
            entry_px = Decimal(str(leg.fill_price if leg.fill_price is not None else leg.entry))
            tol = Decimal(str(smap.min_stop_distance or "0")) or (abs(tp) * Decimal("0.001"))
            lot = Decimal(str(leg.lot))

            m = _txn_by_dealid(txns, leg.broker_position_ref)
            if m is None and not leg.broker_position_ref:
                m = _txn_by_instrument(txns, smap.broker_epic)
            if m is None and require_txn:
                return False

            realized_pl = m.get("pl") if m is not None else None

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

            if realized_pl is None:
                # No broker row — heuristic P&L in INSTRUMENT currency.
                dist = (close_px - entry_px)
                if trade.direction == "SELL":
                    dist = -dist
                realized_pl = dist * lot * vpp
                src = "heuristic"
            else:
                src = "broker"
                # `size` can arrive unsigned; a stop-out is always a loss.
                if outcome == "sl_hit" and realized_pl > 0:
                    realized_pl = -realized_pl

            leg.outcome = outcome
            leg.close_price = close_px
            leg.realized_pl = realized_pl
            leg.status = "closed"
            leg.closed_at = _utcnow()
            if m is not None and m.get("deal_id") and not leg.broker_position_ref:
                leg.broker_position_ref = str(m.get("deal_id"))
            session.add(Event(trade_id=trade.id, leg_id=leg.id, kind="closed",
                              payload={"outcome": outcome, "pl": str(realized_pl), "source": src}))
            await bus.publish(CH_TRADE_EVENT, {"trade_id": trade.id, "leg_id": leg.id,
                                               "outcome": outcome})
            _ev = {"tp_hit": "tp_hit", "sl_hit": "sl_hit", "breakeven": "sl_hit"}.get(outcome)
            if _ev:
                _notify(_ev, {"symbol": trade.symbol, "direction": trade.direction,
                              "price": str(close_px) if close_px is not None else None,
                              "pl": str(realized_pl), "detail": f"TP{leg.tp_index} — {outcome}"})
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
                at = _parse_dt(a.get("date"))
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
                    if ttl_min and ttl_min > 0:
                        age = (_utcnow() - leg.created_at).total_seconds() / 60.0
                        if age > ttl_min:
                            # Never expire something that actually filled+closed.
                            if not await _close_leg(leg, await _transactions(), require_txn=True):
                                try:
                                    await adapter.cancel_order(leg.broker_order_ref)
                                except Exception:
                                    pass
                                leg.status = "expired"
                                leg.outcome = "expired"
                                leg.closed_at = _utcnow()
                                session.add(Event(trade_id=trade.id, leg_id=leg.id,
                                                  kind="expired", payload={"age_min": age}))
                    continue

                # Left the order book: it became a position (fill), filled+closed
                # inside a tick, or was cancelled. Prefer the exact workingOrderId
                # link; fall back to the epic+direction heuristic.
                pos = pos_by_wo.get(leg.broker_order_ref) or _find_fill_heuristic()
                if pos is not None:
                    leg.broker_position_ref = pos.broker_position_ref
                    leg.status = "open"
                    if pos.avg_open_price is not None and leg.fill_price is None:
                        leg.fill_price = pos.avg_open_price
                    linked_refs.add(pos.broker_position_ref)
                    via = "workingOrderId" if pos.working_order_ref == leg.broker_order_ref else "heuristic"
                    session.add(Event(trade_id=trade.id, leg_id=leg.id, kind="filled",
                                      payload={"position": pos.broker_position_ref, "via": via}))
                    _notify("order_filled", {
                        "symbol": trade.symbol, "direction": trade.direction,
                        "price": str(leg.fill_price) if leg.fill_price is not None else None,
                        "detail": f"TP{leg.tp_index} entry filled"})
                elif not await _close_leg(leg, await _transactions(), require_txn=True):
                    leg.status = "cancelled"
                    leg.outcome = "cancelled"
                    leg.closed_at = _utcnow()
                    session.add(Event(trade_id=trade.id, leg_id=leg.id,
                                      kind="cancelled_at_broker", payload={}))
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
            ctx = PositionCtx(
                side=trade.direction,
                entry=Decimal(str(leg.fill_price if leg.fill_price is not None else leg.entry)),
                current_sl=Decimal(str(leg.sl)), current_price=price, tps=tp_levels)
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

        # --- cancel stale limit entries once the trade has progressed ---
        # If a TP has been hit or a stop rule has ratcheted the SL, the remaining
        # unfilled LIMIT entries of THIS trade are stale — price moved the intended
        # way without them (e.g. a 4025-4020 buy where 4025 filled and hit TP1
        # while 4020 never triggered). Cancel them so we don't enter late.
        # Only cancels orders the broker confirms are still cancellable, so an
        # order that just filled is never mislabelled. Configurable per source.
        progressed = bool(effective_tps_hit) or any(l.sl_moved for l in legs)
        if progressed and strat.get("cancel_pending_on_stop", True):
            for leg in legs:
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
                    leg.closed_at = _utcnow()
                    session.add(Event(trade_id=trade.id, leg_id=leg.id, kind="cancelled_by_rule",
                                      payload={"reason": "trade progressed (TP hit / SL ratcheted); stale limit entry"}))
                    _notify("order_cancelled", {
                        "symbol": trade.symbol, "direction": trade.direction,
                        "detail": f"TP{leg.tp_index} limit cancelled — trade progressed"})
                    log.info("trade %s leg %s cancelled — stale limit after progress", trade.id, leg.id)

        # Persist the broker activity audit for this trade (best-effort).
        try:
            await _audit_activities()
        except Exception as exc:
            log.warning("activity audit failed (trade %s): %s", trade.id, exc)

        # roll up trade
        remaining = (await session.execute(select(Leg).where(
            Leg.trade_id == trade.id, Leg.status.in_(OPEN_LEG)))).scalars().all()
        closed = (await session.execute(select(Leg).where(
            Leg.trade_id == trade.id, Leg.status == "closed"))).scalars().all()
        trade.realized_pl = sum((Decimal(str(l.realized_pl)) for l in closed
                                 if l.realized_pl is not None), Decimal("0"))
        if not remaining:
            was_closed = trade.status == "closed"
            trade.status = "closed"
            if not was_closed:
                _notify("trade_closed", {"symbol": trade.symbol, "direction": trade.direction,
                                         "pl": str(trade.realized_pl) if trade.realized_pl is not None else None})
                await _analyze_outcome(session, trade, ai_cfg)
        elif closed:
            trade.status = "partial"
    except AuthError:
        # Session went bad — drop it so the next tick logs in fresh.
        await _evict_adapter(trade.account_id)
        raise


async def tick() -> None:
    async with Session()() as session:
        ai_cfg = await ai_service.load_config(session)
        trades = await _open_trades(session)
        for trade in trades:
            try:
                await _process_trade(session, trade, ai_cfg)
            except Exception as exc:
                log.warning("trade %s tick failed: %s", trade.id, exc)
        await session.commit()


async def main() -> None:
    await init_models()
    asyncio.create_task(run_health_server("monitor", bus, port=8080))
    log.info("monitor loop every %ss", settings.monitor_interval)
    while True:
        try:
            await tick()
        except Exception as exc:
            log.exception("tick failed: %s", exc)
        await asyncio.sleep(settings.monitor_interval)


if __name__ == "__main__":
    asyncio.run(main())
