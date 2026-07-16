"""Executor service.

Consumes validated signals, plans the fanout per enabled account, sizes each
leg, and places orders through the broker gateway (rate-paced). Every leg and
decision is written to the ledger before and after the broker round-trip so a
crash never loses track of real money.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from beacon_core.ai import service as ai_service
from beacon_core.bus import Bus
from beacon_core.settings_store import get_setting
from beacon_core.config import (CH_SIGNAL_VALID, CH_TRADE_OPENED, get_settings,
                                effective_entry_ttl_min)
from beacon_core.logging import get_logger
from beacon_core.health import run_health_server
from beacon_core.db.base import Session, init_models
from beacon_core.db.models import (Account, Event, Leg, Signal, Source, Trade,
                                   ExecutionStrategy)
from beacon_core.execution import strategy as ST
from beacon_core.brokers import build_adapter, symbol_map
from beacon_core.brokers import fx
from beacon_core.tasks import spawn_bg
from beacon_core.timeutil import utcnow
from beacon_core.brokers.types import (OrderSide, OrderStatus, OrderType, PlaceOrderRequest)
from beacon_core.parsing.models import ParsedSignal
from beacon_core.execution.planner import build_plan, DEFAULT_PLANNER
from beacon_core.execution.guard import (should_auto_execute, risk_limit_reason,
                                          DEFAULT_RISK_LIMITS)
from beacon_core.execution.trend_filter import trend_filter_cfg, decide as trend_decide
from beacon_core.risk.sizing import (RiskConfig, InstrumentSpec, size_legs,
                                      plan_total_risk, cap_total_risk)
from beacon_core.ta import capture as ta_capture
from beacon_core.trading_hours import service as th_service
from beacon_core.ta.registry import TF_RESOLUTION
from beacon_core.ta.indicators import ema as _ema, ema_full as _ema_full, atr as _atr
from beacon_core import notifications as notify

log = get_logger("executor")
settings = get_settings()
bus = Bus()


def _notify(event_id: str, ctx: dict) -> None:
    """Fire-and-forget a notification with its own DB session. Best-effort — a
    notification must never affect execution, so failures are swallowed."""
    async def _run():
        try:
            async with Session()() as s:
                await notify.notify(s, event_id, ctx)
        except Exception as exc:                 # pragma: no cover - defensive
            log.debug("notify %s failed: %s", event_id, exc)
    spawn_bg(_run())


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
    spawn_bg(_run())


def _to_parsed(sig: Signal) -> ParsedSignal:
    return ParsedSignal(
        symbol=sig.symbol, direction=sig.direction,
        entry_from=Decimal(str(sig.entry_from)), entry_to=Decimal(str(sig.entry_to)),
        sl=Decimal(str(sig.sl)), tps=[Decimal(str(t)) for t in sig.tps],
        order_type_hint=sig.order_type, raw_text=sig.raw_text or "",
    )


async def _trend_read(adapter, epic: str, timeframe: str, ema_period: int,
                      price: float, slope_lookback: int = 0):
    """(above, slope, dist_atr) for the trend EMA at `timeframe`, or (None,None,
    None) on any failure (fail-open — a missing indicator never blocks a trade).
    above=price>EMA (#48); slope=EMA_now − EMA `slope_lookback` bars ago (#79);
    dist_atr=|price−EMA| in ATR(14) units (#79)."""
    resolution = TF_RESOLUTION.get(timeframe)
    if not resolution:
        return None, None, None
    try:
        bars = await adapter.get_bars(epic, resolution, max_bars=250)
    except Exception as exc:
        log.info("trend-filter bars failed (%s/%s): %s", epic, resolution, exc)
        return None, None, None
    highs = [float(b["h"]) for b in bars if b.get("h") is not None]
    lows = [float(b["l"]) for b in bars if b.get("l") is not None]
    closes = [float(b["c"]) for b in bars if b.get("c") is not None]
    series = _ema_full(closes, int(ema_period))
    val = series[-1] if series else None
    if val is None:
        return None, None, None
    above = price > val
    slope = None
    if slope_lookback > 0 and len(series) > slope_lookback \
            and series[-1 - slope_lookback] is not None:
        slope = series[-1] - series[-1 - slope_lookback]
    dist_atr = None
    if len(highs) == len(closes) == len(lows) and len(closes) >= 15:
        a = _atr(highs, lows, closes, 14)
        if a and a > 0:
            dist_atr = abs(price - val) / a
    return above, slope, dist_atr


async def _accounts_for(session, source: Source):
    ids = source.account_map or []
    if not ids:
        return []
    rows = (await session.execute(
        select(Account).where(Account.id.in_(ids), Account.enabled == True))).scalars().all()
    return rows


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
            sig.status = "skipped"          # terminal: not the re-drive sweep's job (#38)
            await session.commit()
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
            sig.status = "skipped"          # terminal: nothing to re-drive (#38)
            await session.commit()
            return

        # --- news-blackout entry gate (#77) ---
        # Block NEW entries inside a high-impact news window (tiered: -30/+15 for
        # CPI/NFP/FOMC-grade, ±3 otherwise). Market-wide, so gate once per signal
        # before fanning out to accounts; open positions are untouched. Terminal
        # SKIP (a post-print re-entry would just chase the spike). Config-driven
        # (trading_hours.news.gate_entries) and fail-open.
        blackout = await th_service.entry_blackout(session)
        if blackout:
            log.warning("signal %s: SKIP news blackout — %s (%s, T%+dm)", signal_id,
                        blackout.get("title"), blackout.get("tier"), -blackout.get("in_min", 0))
            sig.status = "skipped"
            sig.reject_reason = ("news_blackout: %s" % (blackout.get("title") or "high-impact"))[:128]
            session.add(Event(kind="entry_filtered", payload={
                "signal_id": sig.id, "reason": "news_blackout",
                "event": blackout.get("title"), "impact": blackout.get("impact"),
                "tier": blackout.get("tier"), "minutes_to_event": blackout.get("in_min")}))
            await session.commit()
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
    spawn_bg(_capture_features_bg(signal_id, accounts[0].id))


async def _capture_features_bg(signal_id: int, account_id: int) -> None:
    """Best-effort: capture the signal-time multi-timeframe TA snapshot."""
    try:
        async with Session()() as session:
            sig = await session.get(Signal, signal_id)
            acct = await session.get(Account, account_id)
            if not sig or not acct:
                return
            broker, adapter = await build_adapter(session, acct)
            smap = await symbol_map(session, broker.id, sig.symbol) if broker else None
            if not broker or not smap:
                await adapter.aclose()
                return
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

    broker, adapter = await build_adapter(session, acct)   # trade on the mapped account
    smap = await symbol_map(session, broker.id, parsed.symbol)
    if not smap:
        log.warning("no symbol map for %s on broker %s", parsed.symbol, broker.id)
        await adapter.aclose()
        return
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

        # --- resolve the per-(account, source) ExecutionStrategy (#84) ---
        # One strategy carries the three pillars (entry / filtration / exit). The
        # most-specific enabled scope wins; a missing pillar falls back to the
        # global/source default, so 'no strategy' == today's behaviour.
        _strategies = (await session.execute(select(ExecutionStrategy))).scalars().all()
        strategy = ST.resolve_strategy(_strategies, acct.id, sig.source_id)
        _entry_filters = ST.resolve_entry_filters(
            strategy, global_filters=await get_setting(session, "entry_filters", {}))

        # --- trend-alignment entry filter (#48/#79; filtration pillar) ---
        # Counter-trend entries (direction fighting the higher-TF trend) held ~95%
        # of the book's realized loss. Skip or de-size them. Fail-open.
        trend_size_factor = Decimal("1")
        tf_cfg = trend_filter_cfg(_entry_filters)
        if tf_cfg.get("enabled"):
            _above, _slope, _dist = await _trend_read(
                adapter, smap.broker_epic, tf_cfg.get("timeframe", "4h"),
                int(tf_cfg.get("ema_period", 200)), float(current),
                slope_lookback=int(tf_cfg.get("slope_lookback", 0) or 0))
            _htf = None
            if tf_cfg.get("require_htf_concordance"):     # #79: only fetch when asked
                _htf, _, _ = await _trend_read(
                    adapter, smap.broker_epic, tf_cfg.get("htf_timeframe", "1h"),
                    int(tf_cfg.get("ema_period", 200)), float(current))
            _action, _factor, _aligned = trend_decide(
                tf_cfg, parsed.direction, _above,
                slope=_slope, dist_atr=_dist, htf_above=_htf)
            if _action == "skip":
                log.info("signal %s acct %s: SKIP counter-trend (%s EMA%s)",
                         sig.id, acct.id, tf_cfg.get("timeframe"), tf_cfg.get("ema_period"))
                session.add(Event(kind="entry_filtered",
                                  payload={"signal_id": sig.id, "account_id": acct.id,
                                           "reason": "counter_trend", "aligned": False,
                                           "timeframe": tf_cfg.get("timeframe"),
                                           "ema_period": tf_cfg.get("ema_period")}))
                await session.commit()
                return
            trend_size_factor = Decimal(str(_factor))   # <1 only for de-sized counter-trend

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

        # Entry-strategy pillar (#84): global planner defaults, overlaid with the
        # per-(account,source) entry_policy (chase guard #67 + TTL). No strategy ->
        # exactly the global `planner` setting as before.
        _global_planner = {**DEFAULT_PLANNER, **(await get_setting(session, "planner", {}) or {})}
        planner_cfg = ST.entry_policy(
            strategy, global_planner=_global_planner,
            source_ttl=(source.strategy or {}).get("entry_ttl_minutes") if source else None)
        # Default 0.5 (50%): catches parse-artifact TPs (e.g. tp=1530 vs gold ~4180,
        # ~60% away) while never tripping a real target. Tune via the entry policy.
        max_tp_pct = Decimal(str(planner_cfg.get("max_tp_distance_pct", "0.5")))
        plan = build_plan(
            parsed, current_price=current,
            candle_high=candle_high, candle_low=candle_low,
            min_stop_distance=smap.min_stop_distance,
            max_tp_distance_pct=max_tp_pct if max_tp_pct > 0 else None,
            honor_market_hint=bool(planner_cfg.get("honor_market_hint", True)),
            chase_tolerance_r=Decimal(str(planner_cfg.get("chase_tolerance_r", "0.25"))),
            chase_tolerance_atr=Decimal(str(planner_cfg.get("chase_tolerance_atr", "0"))),
            beyond_tolerance=str(planner_cfg.get("beyond_tolerance", "limit")),
        )
        # Audit the chase-guard decision (#67) whenever it prevented a chase —
        # a MARKET hint rested as a LIMIT, or was skipped — so a bad-fill-avoided
        # is visible in the Activity feed, not silent.
        _guarded = [d for d in plan.entry_decisions if d.get("decision") in ("limit", "skip")]
        if _guarded:
            session.add(Event(kind="entry_chase_guard",
                              payload={"signal_id": sig.id, "account_id": acct.id,
                                       "current_price": str(current), "decisions": _guarded}))
        # Session risk multiplier (#81): de-size entries in the higher-loss
        # London/NY overlap window while keeping London/Asian full. Config-driven
        # (trading_hours.sessions[].risk_mult); fail-open x1.0.
        session_size_factor = Decimal(str(await th_service.session_risk_multiplier(session)))

        # --- filtration rules (#84 pillar): extensible skip / de-size / up-size ---
        # The per-(account,source) strategy's rule set (e.g. inside-FVG -> x2, an
        # NY-overlap -> x0.5) can reject or scale the trade. Rules whose condition
        # inputs aren't available yet are no-ops (fail-open), so richer conditions
        # can be added without wiring risk here.
        filter_factor = Decimal("1")
        _frules = (_entry_filters or {}).get("rules") or []
        if _frules:
            _active = await th_service.active_sessions(session)
            _ff, _skip, _reasons = ST.apply_filter_rules(_frules, {"sessions": _active})
            if _skip:
                log.info("signal %s acct %s: SKIP by filtration (%s)", sig.id, acct.id, _reasons)
                session.add(Event(kind="entry_filtered", payload={
                    "signal_id": sig.id, "account_id": acct.id,
                    "reason": "filtration_skip", "rules": _reasons}))
                await session.commit()
                return
            filter_factor = Decimal(str(_ff))

        risk = RiskConfig.from_dict(source.risk_config or acct.risk_config or {})
        size_factor = trend_size_factor * session_size_factor * filter_factor  # combined
        if size_factor != 1:
            risk.value = risk.value * size_factor
            if risk.per_tp_percent:
                risk.per_tp_percent = {k: v * size_factor
                                       for k, v in risk.per_tp_percent.items()}
        if trend_size_factor < 1:                       # counter-trend de-size (#48)
            log.info("signal %s acct %s: de-sized counter-trend x%s",
                     sig.id, acct.id, trend_size_factor)
            session.add(Event(kind="entry_filtered",
                              payload={"signal_id": sig.id, "account_id": acct.id,
                                       "reason": "counter_trend_desize", "aligned": False,
                                       "factor": str(trend_size_factor)}))
        if session_size_factor < 1:                     # session concentration de-size (#81)
            log.info("signal %s acct %s: de-sized session x%s",
                     sig.id, acct.id, session_size_factor)
            session.add(Event(kind="entry_filtered",
                              payload={"signal_id": sig.id, "account_id": acct.id,
                                       "reason": "session_desize",
                                       "factor": str(session_size_factor)}))
        if filter_factor != 1:                           # filtration scale (#84)
            log.info("signal %s acct %s: filtration scale x%s", sig.id, acct.id, filter_factor)
            session.add(Event(kind="entry_filtered",
                              payload={"signal_id": sig.id, "account_id": acct.id,
                                       "reason": "filtration_scale", "factor": str(filter_factor)}))
        instrument = InstrumentSpec(
            value_per_point=Decimal(str(smap.value_per_point)),
            min_lot=Decimal(str(smap.min_lot)),
            lot_step=Decimal(str(smap.lot_step)),
        )
        size_legs(plan.legs, equity=equity, risk=risk, instrument=instrument,
                  fx_factor=fx_factor)

        # Risk-limit config, loaded here because it also carries the per-signal cap.
        rl_cfg = await get_setting(session, "risk_limits", None)
        if not rl_cfg:                              # never configured -> fail SAFE, not open
            rl_cfg = dict(DEFAULT_RISK_LIMITS)
            log.warning("RISK-LIMITS-DEFAULTED: no risk_limits setting; applying "
                        "conservative defaults (%s)", rl_cfg)

        # --- per-signal risk cap (#78) ---
        # Bound this signal's whole fanout (every entry × TP leg) to
        # max_signal_risk_pct of equity, scaling all legs down proportionally. A
        # per_tp allocation risks each leg independently, so a 2-entry × 5-TP
        # signal can stack to several × the intended single-unit risk; this caps
        # it at the source (complements the #77 news gate and the #73 breaker).
        try:
            _cap_pct = Decimal(str(rl_cfg.get("max_signal_risk_pct", 0) or 0))
        except (ArithmeticError, ValueError, TypeError):
            _cap_pct = Decimal(0)
        if _cap_pct > 0:
            _cap = equity * _cap_pct / Decimal(100)
            _before = plan_total_risk(plan.legs)
            if _cap > 0 and _before > _cap:
                _after = cap_total_risk(plan.legs, cap=_cap, instrument=instrument,
                                        fx_factor=fx_factor)
                log.warning("signal %s acct %s: per-signal risk cap %.2f%% of equity — "
                            "scaled planned risk %s -> %s", sig.id, acct.id,
                            float(_cap_pct), _before, _after)
                session.add(Event(kind="entry_filtered", payload={
                    "signal_id": sig.id, "account_id": acct.id, "reason": "risk_cap_scaled",
                    "cap_pct": str(_cap_pct), "planned_before": str(_before),
                    "planned_after": str(_after)}))

        valid = plan.valid_legs
        if not valid:
            log.info("signal %s acct %s: no legs survived sizing/cap", sig.id, acct.id)
            return

        planned_risk = plan_total_risk(plan.legs)   # worst-case loss, account ccy

        # --- Risk-limit enforcement (independent of the AI gate) ---
        # risk_limit_reason() self-gates on cfg (#65): a present row with
        # enabled:false blocks nothing except the explicit kill-switch; a MISSING
        # row uses DEFAULT_RISK_LIMITS above (enabled) so an un-configured install
        # still fails safe. All limits come from the DB-backed `risk_limits`
        # setting — edited only from the Risk page, never hardcoded here.
        if True:
            day_start = utcnow().replace(
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

        # Exit pillar (#84): SNAPSHOT the resolved sl_rules onto the trade —
        # point-in-time, so this trade's A/B arm is frozen at entry and later
        # strategy edits can't rewrite history. strategy.exit_policy -> source
        # default -> global default. Stamp strategy_id for attribution.
        _gstrat = await get_setting(session, "strategy", {}) or {}
        _sl_rules, _origin = ST.exit_sl_rules(
            strategy,
            source_rules=(source.strategy or {}).get("sl_rules") if source else None,
            global_default=_gstrat.get("default_sl_rules"))
        _strategy_id = strategy.id if (strategy is not None and _origin == "strategy") else None

        trade = Trade(signal_id=sig.id, account_id=acct.id, symbol=parsed.symbol,
                      direction=parsed.direction, status="open",
                      planned_risk=planned_risk,
                      sl_rules=_sl_rules, strategy_id=_strategy_id)
        session.add(trade)
        await session.flush()
        if _strategy_id:
            log.info("signal %s acct %s: strategy #%s (%s) applied", sig.id, acct.id,
                     _strategy_id, strategy.label or "override")

        # Broker-enforced expiry for any working (LIMIT/STOP) leg (#40): the entry
        # TTL from the resolved entry policy (planner_cfg carries ttl_minutes from
        # the strategy / source / global), clamped to a safe range so an unfilled
        # entry can't rest as GTC and fill hours late at a stale price.
        good_till = utcnow() + timedelta(
            minutes=effective_entry_ttl_min({"entry_ttl_minutes": planner_cfg.get("ttl_minutes")}))

        placed = 0
        for pleg in valid:
            leg = Leg(trade_id=trade.id, tp_index=pleg.tp_index,
                      order_type=pleg.order_type, entry=pleg.entry, tp=pleg.tp,
                      sl=pleg.sl, lot=pleg.lot, status="pending")
            session.add(leg)
            await session.flush()
            try:
                _is_market = pleg.order_type == "MARKET"
                req = PlaceOrderRequest(
                    broker_symbol=smap.broker_epic,
                    side=OrderSide.BUY if side_buy else OrderSide.SELL,
                    order_type=OrderType.MARKET if _is_market else OrderType.LIMIT,
                    quantity=pleg.lot,
                    limit_price=None if _is_market else pleg.entry,
                    stop_loss=pleg.sl, take_profit=pleg.tp,
                    # Broker-enforced TTL for working orders (#40) — never GTC.
                    good_till=None if _is_market else good_till,
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


# --- stranded-signal re-drive (backstop for the in-flight at-most-once gap, #38)
_REDRIVE_GRACE_SEC = 60        # older than a normal handle -> not still in flight
_REDRIVE_INTERVAL_SEC = 30     # one sweep per N seconds (never storms the queue)
_REDRIVE_BATCH = 50            # cap per pass


async def _redrive_stranded_signals() -> None:
    """Re-enqueue live signals that were validated but never became trades — the
    residual crash/redeploy window between BRPOP and commit that #34's durable
    queue can't cover (the message was already popped). Redelivery is safe:
    handle_signal short-circuits an executed signal and the per-(signal,account)
    guard prevents a double-place. Un-executable signals are marked 'skipped'
    (not 'validated') so they never re-drive."""
    while True:
        try:
            await asyncio.sleep(_REDRIVE_INTERVAL_SEC)
            _cutoff = utcnow() - timedelta(seconds=_REDRIVE_GRACE_SEC)
            async with Session()() as session:
                _stranded = (await session.execute(
                    select(Signal.id).where(
                        Signal.status == "validated",
                        Signal.created_at < _cutoff,
                        ~select(Trade.id).where(Trade.signal_id == Signal.id).exists(),
                    ).limit(_REDRIVE_BATCH))).scalars().all()
            for _sid in _stranded:
                await bus.enqueue(CH_SIGNAL_VALID, {"signal_id": _sid})
            if _stranded:
                log.warning("re-drove %s stranded validated signal(s): %s",
                            len(_stranded), list(_stranded))
        except Exception as exc:               # a sweep failure must never kill the worker
            log.warning("stranded-signal re-drive sweep failed: %s", exc)


async def main() -> None:
    await init_models()
    spawn_bg(run_health_server("executor", bus, port=8080))
    spawn_bg(_redrive_stranded_signals())
    log.info("executor consuming %s (durable queue)", CH_SIGNAL_VALID)
    # Durable at-least-once: a signal enqueued while we're mid-handle / restarting
    # waits in Redis and is delivered on return (redelivery is safe — handle_signal
    # short-circuits an already-executed signal). Self-heals on Redis drops.
    async for msg in bus.consume_queue(CH_SIGNAL_VALID):
        sid = msg.get("signal_id")
        if sid is None:
            continue
        try:
            await handle_signal(int(sid))
        except Exception as exc:
            log.exception("handle_signal(%s) failed: %s", sid, exc)


if __name__ == "__main__":
    asyncio.run(main())
