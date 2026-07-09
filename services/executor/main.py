"""Executor service.

Consumes validated signals, plans the fanout per enabled account, sizes each
leg, and places orders through the broker gateway (rate-paced). Every leg and
decision is written to the ledger before and after the broker round-trip so a
crash never loses track of real money.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

from sqlalchemy import func, select

from beacon_core.ai import service as ai_service
from beacon_core.bus import Bus
from beacon_core.settings_store import get_setting
from beacon_core.config import (CH_SIGNAL_VALID, CH_TRADE_OPENED, get_settings)
from beacon_core.logging import get_logger
from beacon_core.health import run_health_server
from beacon_core.db.base import Session, init_models
from beacon_core.db.models import (Account, Broker, Event, Leg, Signal,
                                   SymbolMap, Source, Trade)
from beacon_core.brokers import get_adapter, resolve_credentials
from beacon_core.brokers import fx
from beacon_core.brokers.types import (OrderSide, OrderStatus, OrderType, PlaceOrderRequest)
from beacon_core.parsing.models import ParsedSignal
from beacon_core.execution.planner import build_plan
from beacon_core.execution.guard import (should_auto_execute, risk_limit_reason,
                                          DEFAULT_RISK_LIMITS)
from beacon_core.risk.sizing import RiskConfig, InstrumentSpec, size_legs, plan_total_risk
from beacon_core.ta import capture as ta_capture
from beacon_core import notifications as notify

log = get_logger("executor")
settings = get_settings()
bus = Bus()
_BG_TASKS: set = set()          # strong refs to fire-and-forget background tasks


def _notify(event_id: str, ctx: dict) -> None:
    """Fire-and-forget a notification with its own DB session. Best-effort — a
    notification must never affect execution, so failures are swallowed."""
    async def _run():
        try:
            async with Session()() as s:
                await notify.notify(s, event_id, ctx)
        except Exception as exc:                 # pragma: no cover - defensive
            log.debug("notify %s failed: %s", event_id, exc)
    t = asyncio.create_task(_run())
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)


def _review_bg(signal_id, account_id, source_id, plan_dict) -> None:
    """Background (non-blocking) execution review: record the AI's opinion after
    the order is placed, so it never adds latency to the hot path."""
    async def _run():
        try:
            async with Session()() as s:
                sig2 = await s.get(Signal, signal_id)
                src2 = await s.get(Source, source_id) if source_id else None
                if sig2 is not None:
                    await ai_service.assess_execution(s, sig2, src2, plan_dict, account_id)
                    await s.commit()
        except Exception as exc:                     # pragma: no cover - defensive
            log.debug("background exec review failed (signal %s): %s", signal_id, exc)
    t = asyncio.create_task(_run())
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)


def _to_parsed(sig: Signal) -> ParsedSignal:
    return ParsedSignal(
        symbol=sig.symbol, direction=sig.direction,
        entry_from=Decimal(str(sig.entry_from)), entry_to=Decimal(str(sig.entry_to)),
        sl=Decimal(str(sig.sl)), tps=[Decimal(str(t)) for t in sig.tps],
        order_type_hint=sig.order_type, raw_text=sig.raw_text or "",
    )


async def _accounts_for(session, source: Source):
    ids = source.account_map or []
    if not ids:
        return []
    rows = (await session.execute(
        select(Account).where(Account.id.in_(ids), Account.enabled == True))).scalars().all()
    return rows


async def _symbol_map(session, broker_id: int, symbol: str):
    return (await session.execute(select(SymbolMap).where(
        SymbolMap.broker_id == broker_id,
        SymbolMap.internal_symbol == symbol))).scalar_one_or_none()


async def handle_signal(signal_id: int) -> None:
    async with Session()() as session:
        sig = await session.get(Signal, signal_id)
        if not sig:
            return
        # Idempotency: never re-place a signal that already executed (re-delivery
        # or internal retry must not double-place real orders).
        if sig.status == "executed":
            log.info("signal %s already executed; skipping re-delivery", signal_id)
            return
        source = await session.get(Source, sig.source_id) if sig.source_id else None
        if not source or not source.enabled_for_trading:
            log.info("signal %s: source not enabled for trading; skipping", signal_id)
            return

        # Trust gate: untrusted / blocklisted sources do not auto-place live orders
        # (override per-source via strategy.allow_untrusted_live).
        allow_untrusted = bool((source.strategy or {}).get("allow_untrusted_live"))
        ok, block = should_auto_execute(
            enabled_for_trading=source.enabled_for_trading, is_trusted=source.is_trusted,
            name=source.name, allow_untrusted=allow_untrusted)
        if not ok:
            log.warning("signal %s: NOT auto-executing — %s (source '%s')",
                        signal_id, block, source.name)
            sig.status = "blocked"
            sig.reject_reason = (block or "blocked")[:128]
            session.add(Event(kind="blocked_untrusted",
                              payload={"signal_id": sig.id, "source_id": source.id,
                                       "reason": block}))
            await session.commit()
            return

        accounts = await _accounts_for(session, source)
        if not accounts:
            log.info("signal %s: no enabled accounts mapped", signal_id)
            return

        _entry = str(sig.entry_from) if sig.entry_from is not None else None
        if sig.entry_to is not None and sig.entry_to != sig.entry_from:
            _entry = f"{sig.entry_from}–{sig.entry_to}"
        _notify("new_signal", {
            "symbol": sig.symbol, "direction": sig.direction, "entry": _entry,
            "sl": sig.sl, "tp": ", ".join(sig.tps) if sig.tps else None,
            "source": source.name if source else None})

        parsed = _to_parsed(sig)
        ai_cfg = await ai_service.load_config(session)

        for acct in accounts:
            await _execute_on_account(session, sig, parsed, source, acct, ai_cfg)
        sig.status = "executed"
        await session.commit()

    # TA snapshot for later analysis — fired in the background AFTER placement so
    # it adds zero execution latency. One row per signal (own DB session).
    task = asyncio.create_task(_capture_features_bg(signal_id, accounts[0].id))
    _BG_TASKS.add(task)                     # keep a ref so it isn't GC'd mid-run
    task.add_done_callback(_BG_TASKS.discard)


async def _capture_features_bg(signal_id: int, account_id: int) -> None:
    """Best-effort: capture the signal-time multi-timeframe TA snapshot."""
    try:
        async with Session()() as session:
            sig = await session.get(Signal, signal_id)
            acct = await session.get(Account, account_id)
            if not sig or not acct:
                return
            broker = await session.get(Broker, acct.broker_id)
            smap = await _symbol_map(session, broker.id, sig.symbol) if broker else None
            if not broker or not smap:
                return
            creds = resolve_credentials(broker.credentials_ref)
            creds.setdefault("is_demo", broker.is_demo)
            creds["account_id"] = acct.broker_account_id
            adapter = get_adapter(broker.type, creds)
            try:
                await ta_capture.capture_for_signal(session, sig, adapter, smap)
                await session.commit()
            finally:
                await adapter.aclose()
    except Exception as exc:                       # never let capture affect the worker
        log.warning("TA feature capture failed (signal %s): %s", signal_id, exc)


async def _execute_on_account(session, sig, parsed, source, acct,
                              ai_cfg=None) -> None:
    # Idempotency: one trade per (signal, account). If one already exists this
    # signal was already placed here — skip rather than double-place.
    dup = (await session.execute(select(Trade.id).where(
        Trade.signal_id == sig.id, Trade.account_id == acct.id))).first()
    if dup:
        log.info("signal %s acct %s already has trade %s; skipping", sig.id, acct.id, dup[0])
        return

    broker = await session.get(Broker, acct.broker_id)
    smap = await _symbol_map(session, broker.id, parsed.symbol)
    if not smap:
        log.warning("no symbol map for %s on broker %s", parsed.symbol, broker.id)
        return

    creds = resolve_credentials(broker.credentials_ref)
    creds.setdefault("is_demo", broker.is_demo)
    creds["account_id"] = acct.broker_account_id   # trade on the mapped account
    adapter = get_adapter(broker.type, creds)
    try:
        info = await adapter.get_account_info()
        equity = info.balance or Decimal("0")
        account_ccy = info.currency or acct.currency or "USD"
        quote = await adapter.get_quote(smap.broker_epic)
        side_buy = parsed.direction == "BUY"
        current = (quote.offer if side_buy else quote.bid) or quote.last_price
        if current is None:
            log.warning("no price for %s; skipping account %s", smap.broker_epic, acct.id)
            return

        # Current-candle range: a leg whose entry the candle has already crossed is
        # opened MARKET (see build_plan). Best-effort; falls back to the live price.
        candle_high = candle_low = None
        try:
            bars = await adapter.get_bars(smap.broker_epic, "MINUTE", max_bars=2)
            if bars:
                last = bars[-1]
                candle_high = Decimal(str(last["h"])) if last.get("h") is not None else None
                candle_low = Decimal(str(last["l"])) if last.get("l") is not None else None
        except Exception as exc:
            log.info("candle fetch failed for %s: %s", smap.broker_epic, exc)

        # Instrument currency comes from the broker market; convert account->instr.
        instrument_ccy = quote.currency or "USD"
        fx_overrides = await get_setting(session, "fx", {}) or {}
        try:
            fx_factor = await fx.factor(adapter, account_ccy, instrument_ccy,
                                        overrides=fx_overrides)
        except fx.FxUnavailable as exc:
            log.warning("signal %s acct %s: %s — skipping (won't mis-size)",
                        sig.id, acct.id, exc)
            session.add(Event(kind="fx_unavailable",
                              payload={"account_id": acct.id, "error": str(exc)}))
            return

        planner_cfg = await get_setting(session, "planner", {}) or {}
        # Default 0.5 (50%): catches parse-artifact TPs (e.g. tp=1530 vs gold ~4180,
        # ~60% away) while never tripping a real target. Tune via the `planner` setting.
        max_tp_pct = Decimal(str(planner_cfg.get("max_tp_distance_pct", "0.5")))
        plan = build_plan(
            parsed, current_price=current,
            candle_high=candle_high, candle_low=candle_low,
            min_stop_distance=smap.min_stop_distance,
            max_tp_distance_pct=max_tp_pct if max_tp_pct > 0 else None,
            honor_market_hint=bool(planner_cfg.get("honor_market_hint", True)),
        )
        risk = RiskConfig.from_dict(source.risk_config or acct.risk_config or {})
        instrument = InstrumentSpec(
            value_per_point=Decimal(str(smap.value_per_point)),
            min_lot=Decimal(str(smap.min_lot)),
            lot_step=Decimal(str(smap.lot_step)),
        )
        size_legs(plan.legs, equity=equity, risk=risk, instrument=instrument,
                  fx_factor=fx_factor)
        valid = plan.valid_legs
        if not valid:
            log.info("signal %s acct %s: no legs survived sizing", sig.id, acct.id)
            return

        planned_risk = plan_total_risk(plan.legs)   # worst-case loss, account ccy

        # --- Risk-limit enforcement (independent of the AI gate) ---
        rl_cfg = await get_setting(session, "risk_limits", None)
        if not rl_cfg:                              # never configured -> fail SAFE, not open
            rl_cfg = dict(DEFAULT_RISK_LIMITS)
            log.warning("RISK-LIMITS-DEFAULTED: no risk_limits setting; applying "
                        "conservative defaults (%s)", rl_cfg)
        if rl_cfg.get("enabled"):
            day_start = dt.datetime.now(dt.timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0)
            day_realized = (await session.execute(select(
                func.coalesce(func.sum(Trade.realized_pl), 0)).where(
                Trade.account_id == acct.id, Trade.created_at >= day_start))).scalar()
            open_sym = (await session.execute(select(
                func.coalesce(func.sum(Trade.planned_risk), 0)).where(
                Trade.account_id == acct.id, Trade.symbol == parsed.symbol,
                Trade.status.in_(("open", "partial"))))).scalar()
            open_acct = (await session.execute(select(
                func.coalesce(func.sum(Trade.planned_risk), 0)).where(
                Trade.account_id == acct.id,
                Trade.status.in_(("open", "partial"))))).scalar()
            reason = risk_limit_reason(
                planned_risk=planned_risk, day_realized=day_realized,
                open_risk_symbol=open_sym, open_risk_account=open_acct, cfg=rl_cfg)
            if reason:
                session.add(Event(kind="risk_blocked",
                                  payload={"signal_id": sig.id, "account_id": acct.id,
                                           "planned_risk": str(planned_risk), "reason": reason}))
                await session.commit()
                log.warning("signal %s acct %s: RISK-LIMIT BLOCK — %s",
                            sig.id, acct.id, reason)
                return

        # --- AI execution review ---
        review_on = ai_cfg is not None and ai_cfg.ready and ai_cfg.review_execution
        plan_dict = None
        if review_on:
            risk_pct = (float(planned_risk) / float(equity) * 100.0) if equity else None
            plan_dict = {
                "account_currency": account_ccy, "equity": str(equity),
                # Currency/sizing context so the AI reasons in the right units and
                # doesn't mistake a correctly-sized position for a leverage error:
                # equity/risk are in ACCOUNT currency; value_per_point is in the
                # INSTRUMENT currency; fx_factor converts account -> instrument.
                "instrument_currency": instrument_ccy,
                "value_per_point": str(smap.value_per_point),
                "fx_factor": str(fx_factor),
                "planned_risk": str(planned_risk),
                "risk_pct": round(risk_pct, 3) if risk_pct is not None else None,
                "legs": [{"tp_index": l.tp_index, "entry": str(l.entry),
                          "tp": str(l.tp), "sl": str(l.sl), "lot": str(l.lot)}
                         for l in valid],
            }
            # BLOCK mode only: wait for the review (and optionally gate) before
            # placing. background/off do not hold up the order.
            if ai_cfg.review_mode == "block":
                try:
                    a = await ai_service.assess_execution(session, sig, source, plan_dict,
                                                          acct.id, cfg=ai_cfg)
                    await session.commit()
                    if (a is not None and ai_cfg.gate_execution and a.verdict == "reject"
                            and (a.confidence is None
                                 or float(a.confidence) >= ai_cfg.min_confidence)):
                        session.add(Event(kind="ai_blocked",
                                          payload={"signal_id": sig.id, "account_id": acct.id,
                                                   "rationale": a.rationale}))
                        await session.commit()
                        log.warning("signal %s acct %s: BLOCKED by AI: %s",
                                    sig.id, acct.id, a.rationale)
                        return
                except Exception as exc:             # AI must never break execution
                    log.warning("AI execution review failed: %s", exc)

        trade = Trade(signal_id=sig.id, account_id=acct.id, symbol=parsed.symbol,
                      direction=parsed.direction, status="open",
                      planned_risk=planned_risk)
        session.add(trade)
        await session.flush()

        placed = 0
        for pleg in valid:
            leg = Leg(trade_id=trade.id, tp_index=pleg.tp_index,
                      order_type=pleg.order_type, entry=pleg.entry, tp=pleg.tp,
                      sl=pleg.sl, lot=pleg.lot, status="pending")
            session.add(leg)
            await session.flush()
            try:
                req = PlaceOrderRequest(
                    broker_symbol=smap.broker_epic,
                    side=OrderSide.BUY if side_buy else OrderSide.SELL,
                    order_type=OrderType.MARKET if pleg.order_type == "MARKET" else OrderType.LIMIT,
                    quantity=pleg.lot,
                    limit_price=None if pleg.order_type == "MARKET" else pleg.entry,
                    stop_loss=pleg.sl, take_profit=pleg.tp,
                )
                res = await adapter.place_order(req)
                if res.status == OrderStatus.REJECTED:
                    # Broker declined the order (market closed, risk check, min
                    # size, bad epic, …). Make it visible instead of silently
                    # leaving the leg 'pending'.
                    leg.status = "rejected"
                    leg.outcome = "rejected"
                    session.add(Event(trade_id=trade.id, leg_id=leg.id, kind="reject",
                                      payload={"ref": res.broker_order_ref,
                                               "reason": res.rejection_reason}))
                    log.warning("signal %s acct %s leg %s REJECTED by broker: %s",
                                sig.id, acct.id, leg.id, res.rejection_reason)
                else:
                    if pleg.order_type == "MARKET":
                        leg.broker_position_ref = res.broker_order_ref
                        leg.status = "open" if res.status == OrderStatus.FILLED else "pending"
                        leg.fill_price = res.fill_price
                    else:
                        leg.broker_order_ref = res.broker_order_ref
                        leg.status = "working"
                    session.add(Event(trade_id=trade.id, leg_id=leg.id, kind="placed",
                                      payload={"ref": res.broker_order_ref,
                                               "status": res.status.value}))
                    placed += 1
            except Exception as exc:              # one leg failing must not sink the rest
                leg.status = "rejected"
                session.add(Event(trade_id=trade.id, leg_id=leg.id, kind="reject",
                                  payload={"error": str(exc)[:300]}))
                log.warning("leg place failed (trade %s): %s", trade.id, exc)
            await asyncio.sleep(1.0 / max(settings.broker_rate_per_sec, 0.1))

        await session.commit()
        await bus.publish(CH_TRADE_OPENED, {"trade_id": trade.id, "account_id": acct.id,
                                            "placed": placed})
        if placed:
            _notify("order_placed", {
                "symbol": sig.symbol, "direction": sig.direction, "account": acct.name,
                "detail": f"{placed}/{len(valid)} legs placed"})
        # Non-blocking review mode: run the AI for the record after placing.
        if review_on and ai_cfg.review_mode == "background" and placed:
            _review_bg(sig.id, acct.id, source.id if source else None, plan_dict)
        log.info("signal %s acct %s: placed %s/%s legs", sig.id, acct.id, placed, len(valid))
    finally:
        await adapter.aclose()


async def main() -> None:
    await init_models()
    asyncio.create_task(run_health_server("executor", bus, port=8080))
    log.info("executor listening on %s", CH_SIGNAL_VALID)
    async for msg in bus.subscribe(CH_SIGNAL_VALID):
        sid = msg.get("signal_id")
        if sid is None:
            continue
        try:
            await handle_signal(int(sid))
        except Exception as exc:
            log.exception("handle_signal(%s) failed: %s", sid, exc)


if __name__ == "__main__":
    asyncio.run(main())
