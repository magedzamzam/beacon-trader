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
from beacon_core.db.models import (Account, Broker, Event, Leg, Signal, Source,
                                   SymbolMap, Trade)
from beacon_core.brokers import get_adapter, resolve_credentials
from beacon_core.brokers.types import AuthError, ModifyPositionRequest
from beacon_core.strategy.rules import PositionCtx, evaluate

log = get_logger("monitor")
settings = get_settings()
bus = Bus()

OPEN_LEG = ("pending", "working", "open")


def _utcnow():
    return dt.datetime.now(dt.timezone.utc)


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
        rules, strat, ttl_min = await _rules_for(session, trade)
        tps_hit = _tps_hit(trade.direction, price, legs)
        # TP price map across ALL legs of the trade, so a ratchet rule that
        # references a TP whose legs already closed still resolves its level.
        _all_legs = (await session.execute(select(Leg).where(
            Leg.trade_id == trade.id))).scalars().all()
        tp_levels = {l.tp_index: Decimal(str(l.tp)) for l in _all_legs}

        # positions already linked to an open leg of this trade (so we don't
        # re-claim them when deciding whether a vanished order was filled).
        linked_refs = {l.broker_position_ref for l in legs
                       if l.status == "open" and l.broker_position_ref}

        def _find_fill():
            """An open position on this epic+direction not yet linked -> a fill."""
            for ref, p in positions.items():
                if ref in linked_refs:
                    continue
                if (p.broker_symbol == smap.broker_epic
                        and str(p.direction).endswith(trade.direction)):
                    return ref
            return None

        vpp = Decimal(str(smap.value_per_point))

        # Broker deal history: fetched lazily (only when a close is detected) and
        # once per tick. It gives the ACTUAL close level and realized P&L in the
        # account currency — the source of truth. A closed leg is matched to a
        # transaction by close level (nearest the leg's TP, else SL) and size,
        # which also correctly labels a fast TP touch the poll interval missed.
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

        def _match_txn(txns, target: Decimal, tol: Decimal, lot: Decimal):
            best = None
            for t in txns:
                if t.get("_used") or t.get("close_level") is None:
                    continue
                if t.get("type") and t["type"] not in ("TRADE", "DEAL", "POSITION"):
                    continue
                sz = t.get("size")
                if (sz is not None and lot is not None
                        and abs(abs(sz) - abs(lot)) > max(abs(lot) * Decimal("0.02"), Decimal("0.01"))):
                    continue
                if abs(t["close_level"] - target) > tol:
                    continue
                if best is None or abs(t["close_level"] - target) < abs(best["close_level"] - target):
                    best = t
            if best is not None:
                best["_used"] = True
            return best

        # --- pass 1: reconcile fills, detect + attribute closes ---
        for leg in legs:
            if leg.status == "working":
                if leg.broker_order_ref not in orders:
                    # Left the book: either filled (a new position appears) or
                    # cancelled/deleted at the broker. Distinguish, don't assume.
                    fill_ref = _find_fill()
                    if fill_ref:
                        leg.broker_position_ref = fill_ref
                        leg.status = "open"
                        linked_refs.add(fill_ref)
                        session.add(Event(trade_id=trade.id, leg_id=leg.id,
                                          kind="filled", payload={"position": fill_ref}))
                    else:
                        leg.status = "cancelled"
                        leg.outcome = "cancelled"
                        leg.closed_at = _utcnow()
                        session.add(Event(trade_id=trade.id, leg_id=leg.id,
                                          kind="cancelled_at_broker", payload={}))
                    continue
                age = (_utcnow() - leg.created_at).total_seconds() / 60.0
                if age > ttl_min:
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

            if (leg.status == "open" and leg.broker_position_ref
                    and leg.broker_position_ref not in positions):
                tp = Decimal(str(leg.tp)); sl = Decimal(str(leg.sl))
                entry_px = Decimal(str(leg.fill_price if leg.fill_price is not None else leg.entry))
                tol = Decimal(str(smap.min_stop_distance or "0")) or (abs(tp) * Decimal("0.001"))
                txns = await _transactions()

                outcome, close_px, pl, src = None, None, None, "heuristic"
                match = _match_txn(txns, tp, tol, Decimal(str(leg.lot)))
                if match is not None:
                    outcome, close_px, pl, src = "tp_hit", match["close_level"], match.get("pl"), "broker"
                else:
                    match = _match_txn(txns, sl, tol, Decimal(str(leg.lot)))
                    if match is not None:
                        outcome, close_px, pl, src = "sl_hit", match["close_level"], match.get("pl"), "broker"

                if outcome is None:
                    # No broker match — fall back to the live-price heuristic.
                    tp_reached = (price >= tp) if trade.direction == "BUY" else (price <= tp)
                    sl_reached = (price <= sl) if trade.direction == "BUY" else (price >= sl)
                    if tp_reached or abs(price - tp) <= tol:
                        outcome, close_px = "tp_hit", tp
                    elif sl_reached or abs(price - sl) <= tol:
                        outcome, close_px = "sl_hit", sl
                    else:
                        outcome, close_px = "manual", price

                if pl is None:   # heuristic P&L is in INSTRUMENT currency
                    dist = (close_px - entry_px)
                    if trade.direction == "SELL":
                        dist = -dist
                    pl = dist * Decimal(str(leg.lot)) * vpp

                leg.outcome = outcome
                leg.close_price = close_px
                leg.realized_pl = pl
                leg.status = "closed"
                leg.closed_at = _utcnow()
                session.add(Event(trade_id=trade.id, leg_id=leg.id, kind="closed",
                                  payload={"outcome": outcome, "pl": str(pl), "source": src}))
                await bus.publish(CH_TRADE_EVENT, {"trade_id": trade.id, "leg_id": leg.id,
                                                   "outcome": outcome})

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
                    log.info("trade %s leg %s SL -> %s", trade.id, leg.id, new_sl)
                except Exception as exc:
                    log.warning("SL move failed (leg %s): %s", leg.id, exc)

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
