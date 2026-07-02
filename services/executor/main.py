"""Executor service.

Consumes validated signals, plans the fanout per enabled account, sizes each
leg, and places orders through the broker gateway (rate-paced). Every leg and
decision is written to the ledger before and after the broker round-trip so a
crash never loses track of real money.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import select

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
from beacon_core.risk.sizing import RiskConfig, InstrumentSpec, size_legs, plan_total_risk

log = get_logger("executor")
settings = get_settings()
bus = Bus()


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
        source = await session.get(Source, sig.source_id) if sig.source_id else None
        if not source or not source.enabled_for_trading:
            log.info("signal %s: source not enabled for trading; skipping", signal_id)
            return

        accounts = await _accounts_for(session, source)
        if not accounts:
            log.info("signal %s: no enabled accounts mapped", signal_id)
            return

        parsed = _to_parsed(sig)
        strat = source.strategy or {}
        order_position_type = strat.get("order_position_type", sig.order_type or "MARKET")

        ai_cfg = await ai_service.load_config(session)

        for acct in accounts:
            await _execute_on_account(session, sig, parsed, source, acct,
                                      order_position_type, ai_cfg)
        sig.status = "executed"
        await session.commit()


async def _execute_on_account(session, sig, parsed, source, acct,
                              order_position_type, ai_cfg=None) -> None:
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

        plan = build_plan(
            parsed, order_position_type=order_position_type, current_price=current,
            min_stop_distance=smap.min_stop_distance,
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

        # --- AI execution review (best-effort; optional hard gate) ---
        if ai_cfg is not None and ai_cfg.ready and ai_cfg.review_execution:
            planned_risk = plan_total_risk(plan.legs)
            risk_pct = (float(planned_risk) / float(equity) * 100.0) if equity else None
            plan_dict = {
                "account_currency": account_ccy, "equity": str(equity),
                "planned_risk": str(planned_risk),
                "risk_pct": round(risk_pct, 3) if risk_pct is not None else None,
                "legs": [{"tp_index": l.tp_index, "entry": str(l.entry),
                          "tp": str(l.tp), "sl": str(l.sl), "lot": str(l.lot)}
                         for l in valid],
            }
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
            except Exception as exc:                 # AI must never break execution
                log.warning("AI execution review failed: %s", exc)

        trade = Trade(signal_id=sig.id, account_id=acct.id, symbol=parsed.symbol,
                      direction=parsed.direction, status="open",
                      planned_risk=plan_total_risk(plan.legs))
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
