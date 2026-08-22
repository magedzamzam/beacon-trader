"""Executor service.

Consumes validated signals, plans the fanout per enabled account, sizes each
leg, and places orders through the broker gateway (rate-paced). Every leg and
decision is written to the ledger before and after the broker round-trip so a
crash never loses track of real money.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
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
                                   ExecutionStrategy, AccountSourceRisk,
                                   StagedEntry, StagedTranche)
from beacon_core.execution import strategy as ST
from beacon_core.analysis import epochs as EP        # filter-rule epoch stamp (#253)
from beacon_core.execution import staging as STG
from beacon_core.execution import placement as PLACE     # broker-refusal recovery (#221)
from beacon_core.execution import sl_override as SLO     # per-channel stop (#249)
from beacon_core.execution import ladder as LAD          # staged-entry ladder (#250)
from beacon_core.brokers import build_adapter, symbol_map
from beacon_core.brokers import fx
from beacon_core.tasks import spawn_bg
from beacon_core.timeutil import utcnow
from beacon_core.brokers.types import (OrderSide, OrderStatus, OrderType, PlaceOrderRequest)
from beacon_core.parsing.models import ParsedSignal
from beacon_core.execution.planner import (build_plan, fanout_order, FanoutPlan,
                                           DEFAULT_PLANNER)
from beacon_core.execution.guard import (should_auto_execute, risk_limit_reason,
                                          soft_breaker_decision, DEFAULT_RISK_LIMITS)
from beacon_core.execution.trend_filter import trend_filter_cfg, decide as trend_decide
from beacon_core.risk.sizing import (RiskConfig, InstrumentSpec, size_legs,
                                      plan_total_risk, cap_total_risk, resolve_risk_config)
from beacon_core.risk import cluster as CL
from beacon_core.ta import capture as ta_capture
from beacon_core.trading_hours import service as th_service
from beacon_core.ta.registry import TF_RESOLUTION
from beacon_core.ta.features import compute_timeframe as _ta_compute
from beacon_core.ta.indicators import (ema as _ema, ema_full as _ema_full,
                                       atr as _atr, adx as _adx_ind)
from beacon_core import notifications as notify
from beacon_core.notifications.throttle import Throttle, suffix as _burst_suffix

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


# A broker failure never arrives alone — a bad epic or a closed market rejects
# every leg of a fanout in the same second. Collapse a burst into one alert that
# says how many it stood for (#180).
_BROKER_ERR = Throttle()
_BROKER_ERR_DETAIL_MAX = 300     # a rejection reason can be an essay


def _broker_error(kind: str, detail: str, *, account=None, symbol=None,
                  account_id=None) -> None:
    """Debounced `broker_error` notification. `kind` scopes the debounce, so a
    storm of rejects collapses but a *different* failure still gets through."""
    ok, suppressed = _BROKER_ERR.allow(f"{kind}:{account_id}:{symbol}")
    if not ok:
        return
    _notify("broker_error", {
        "symbol": symbol, "account": account,
        "detail": _burst_suffix(str(detail)[:_BROKER_ERR_DETAIL_MAX], suppressed)})


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


async def _adx_read(adapter, epic: str, timeframe: str, period: int = 14):
    """Live per-TF ADX {adx, trending} for the adx_regime entry filter (#132), or
    None on any failure (fail-open). Signal features are captured in the background
    AFTER execution, so the live filter can't read a persisted `adx_14` — it
    computes ADX in the hot path, and only when an adx_regime rule references the
    timeframe (so the default install fetches nothing extra)."""
    resolution = TF_RESOLUTION.get(timeframe)
    if not resolution:
        return None
    try:
        bars = await adapter.get_bars(epic, resolution, max_bars=250)
    except Exception as exc:
        log.info("adx bars failed (%s/%s): %s", epic, resolution, exc)
        return None
    highs = [float(b["h"]) for b in bars if b.get("h") is not None]
    lows = [float(b["l"]) for b in bars if b.get("l") is not None]
    closes = [float(b["c"]) for b in bars if b.get("c") is not None]
    if len(highs) == len(closes) == len(lows) and len(closes) >= 15:
        d = _adx_ind(highs, lows, closes, int(period))
        if d and d.get("adx") is not None:
            return {"adx": d["adx"], "trending": bool(d.get("trending"))}
    return None


async def _ta_ctx(adapter, epic: str, rules) -> dict:
    """ctx['ta'][timeframe][instance_key] for every indicator an `indicator`
    filtration rule references (#167) — the generic replacement for `_adx_read`'s
    one-indicator plumbing.

    The rule set says what it needs (`ta_rule_requirements`), so each referenced
    timeframe's bars are fetched EXACTLY ONCE no matter how many rules read it,
    the registry's own `compute` callables produce the values (no duplicated
    math), and a rule set that references no TA fetches nothing at all — the hot
    path stays free on the default install, which is the good property the ADX
    version had and the reason it was worth generalising rather than replacing.

    FAIL-OPEN everywhere: a broker error, an unknown timeframe or a thin series
    just leaves that block out of ctx, and `_match_indicator` reads an absent
    input as "does not match"."""
    reqs = ST.ta_rule_requirements(rules)
    if not reqs:
        return {}
    by_tf: dict[str, list] = {}
    for r in reqs:
        by_tf.setdefault(r["timeframe"], []).append(r)
    out: dict[str, dict] = {}
    for tf, items in by_tf.items():
        resolution = TF_RESOLUTION.get(tf)
        if not resolution:
            continue
        try:
            bars = await adapter.get_bars(epic, resolution, max_bars=250)
        except Exception as exc:
            log.info("filter TA bars failed (%s/%s): %s", epic, resolution, exc)
            continue
        try:
            feats = _ta_compute(bars, None,
                                [{"id": r["id"], "params": r["params"]} for r in items])
        except Exception as exc:
            log.info("filter TA compute failed (%s/%s): %s", epic, tf, exc)
            continue
        block = {k: v for k, v in (feats or {}).items() if not k.startswith("_")}
        if block:
            out[tf] = block
    return out


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
            "channel": source.name if source else None})

        parsed = _to_parsed(sig)
        ai_cfg = await ai_service.load_config(session)

        # #211: the fanout is SEQUENTIAL, so whichever account goes last trades a
        # staler price — measured at $0.62 (0.058R) of adverse fill for the third
        # arm, the same size as the treatment effects being measured and always
        # signed against the same arm. Placing in a per-signal seeded permutation
        # turns that systematic bias into noise the day-block bootstrap already
        # accounts for. Sizing, risk and whether a trade is placed are untouched:
        # this changes only WHICH arm draws the short straw on a given signal.
        _order = {a: i for i, a in
                  enumerate(fanout_order([a.id for a in accounts], sig.id))}
        _fanout_started = time.monotonic()
        for acct in sorted(accounts, key=lambda a: _order.get(a.id, 0)):
            _lag_ms = int(round((time.monotonic() - _fanout_started) * 1000))
            await _execute_on_account(session, sig, parsed, source, acct, ai_cfg,
                                      placement_lag_ms=_lag_ms)
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
                              ai_cfg=None, *, placement_lag_ms=None) -> None:
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

        # --- resolve the per-(account, source) ExecutionStrategy chain (#84/#104) ---
        # One strategy carries the three pillars (entry / filtration / exit).
        # Pillars CASCADE most-specific -> (Any, Any) base, so Strategies is the
        # single source of truth — the legacy global `entry_filters` / `planner`
        # SETTINGS are no longer consulted (they were migrated into the (Any, Any)
        # row); only the built-in code defaults sit underneath.
        _strategies = (await session.execute(select(ExecutionStrategy))).scalars().all()
        _chain = ST.resolve_chain(_strategies, acct.id, sig.source_id)
        strategy = _chain[0] if _chain else None          # attribution
        _entry_filters = ST.resolve_entry_filters(_chain)
        # #253: the epoch this account's filtration is deciding under, stamped onto
        # every filtration event below. Computed from the pillars of the row that
        # SUPPLIED the filters (not chain[0], which may contribute only an exit
        # ladder) and from the rules AS THEY RAN — never read back off the stored
        # `epoch_digest`, because a row written by SQL rather than the API carries a
        # stale one, and stamping that would file the new configuration's removals
        # under the old one's identity. Without a stamp the weekly reconstructs
        # epoch membership from `updated_at`, which pooled three `adx_regime`
        # configurations into one epoch and returned a REMOVES_LOSERS that no
        # single filter ever earned.
        # FAIL OPEN, like the evaluator below (#164): this is a LABEL on a
        # measurement event and nothing on the trading path reads it, so it must
        # never be the reason a signal is lost.
        _frow = ST.entry_filters_row(_chain)
        try:
            _epoch = EP.event_stamp(_entry_filters,
                                    getattr(_frow, "entry_policy", None))
        except Exception as exc:                        # pragma: no cover - defensive
            log.warning("signal %s acct %s: epoch stamp failed: %s", sig.id, acct.id, exc)
            _epoch = {}

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
                                           **_epoch,
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

        # Entry-strategy pillar (#84/#104): built-in planner defaults, then the
        # strategy chain least->most specific (chase guard #67 + TTL). The legacy
        # global `planner` setting is retired — its values live in the (Any, Any)
        # strategy row, so exactly one surface decides the entry policy.
        planner_cfg = ST.entry_policy(
            _chain, global_planner=DEFAULT_PLANNER,
            source_ttl=(source.strategy or {}).get("entry_ttl_minutes") if source else None)

        # --- per-channel stop override (#249) ---------------------------------
        # Replace the channel's stop with one `sl_distance` from the signal's far
        # entry edge. HERE, before the plan is built, because the plan is what
        # sizing reads: lot = risk_cash / |entry - sl|, so a tighter stop trades a
        # LARGER lot at the same cash risk. Applied any later and the lot would be
        # sized against the channel's stop while the order carried ours.
        #
        # `parsed` is built ONCE per signal and fanned across every account, so
        # this REBINDS A COPY (sl_override.apply never mutates). An arm that did
        # not configure an override must keep the channel's stop, or the A/B is
        # comparing something nobody chose.
        _sl_distance = SLO.resolve_distance(planner_cfg)
        if _sl_distance is not None:
            _sl_before = parsed.sl
            # CAP, never FIXED (#252). The live override may only ever TIGHTEN: a
            # signal already carrying a stop tighter than our number is left alone
            # and logged `already_tighter`.
            #
            # This is not a preference, it is the difference between the feature
            # and its opposite. The distance is measured from `entry_to`, the far
            # edge, and on a zone signal that is much closer to the stop than
            # `entry_from` is — median |entry_to - sl| is $5.00 on src 5 against
            # $10.00 from the near edge. Under FIXED, arming src 5 at $7.50 would
            # have WIDENED 93.5% of its stops (and src 7, 77.3%), lowering R per
            # winner — the exact opposite of what arming it is for.
            parsed, _sl_note = SLO.apply(parsed, _sl_distance, mode=SLO.MODE_CAP,
                                         min_stop_distance=smap.min_stop_distance)
            log.info("signal %s acct %s: sl_distance=%s %s (%s -> %s)", sig.id,
                     acct.id, _sl_distance, _sl_note, _sl_before, parsed.sl)
            # Recorded whatever the outcome: an override that fell back to the
            # channel's stop on half the book would otherwise be invisible, and
            # the measurement it exists to produce would be uninterpretable.
            session.add(Event(kind="sl_override", payload={
                "signal_id": sig.id, "account_id": acct.id,
                "source_id": sig.source_id, "note": _sl_note,
                "mode": SLO.MODE_CAP, "distance": str(_sl_distance),
                "sl_before": str(_sl_before), "sl_after": str(parsed.sl),
                "anchor": str(parsed.entry_to),
                "min_stop_distance": (str(smap.min_stop_distance)
                                      if smap.min_stop_distance is not None else None)}))
        # Default 0.5 (50%): catches parse-artifact TPs (e.g. tp=1530 vs gold ~4180,
        # ~60% away) while never tripping a real target. Tune via the entry policy.
        max_tp_pct = Decimal(str(planner_cfg.get("max_tp_distance_pct", "0.5")))
        # --- Staged entry: THE LADDER (#250) ---------------------------------
        # `entry_style: staged` plans the signal as a table of rungs instead of
        # the single-shot fanout. Each row says when to place an order, what
        # kind, at which level and for which target; the rows that fire on the
        # signal go out now and the rest are persisted for the monitor.
        #
        # This replaced a partition/reclaim model with thirteen tuning numbers,
        # none of which anyone had ever changed. The ladder is the config.
        is_staged = str(planner_cfg.get("entry_style") or "") == "staged"
        ladder_rows = None
        if is_staged:
            # The ladder is GLOBAL: which one this signal runs follows from its
            # own shape (single entry level vs a zone, and how many TPs), not from
            # the channel. The strategy only chose to switch staged entry on.
            _grid = LAD.matrix_with_defaults(
                await get_setting(session, "staged_ladders", None))
            ladder_rows = LAD.rows_for(_grid, parsed)
            # The chase guard decides whether the SIGNAL is taken at all, and it
            # must decide that the same way for both entry styles or the A/B is
            # comparing different populations (#155). build_plan is pure and
            # cheap, so ask it: no legs means beyond_tolerance="skip" declined the
            # whole signal, and a laddered account declines it too.
            _control = build_plan(
                parsed, current_price=current,
                candle_high=candle_high, candle_low=candle_low,
                min_stop_distance=smap.min_stop_distance,
                max_tp_distance_pct=max_tp_pct if max_tp_pct > 0 else None,
                honor_market_hint=bool(planner_cfg.get("honor_market_hint", True)),
                chase_tolerance_r=Decimal(str(planner_cfg.get("chase_tolerance_r", "0.25"))),
                chase_tolerance_atr=Decimal(str(planner_cfg.get("chase_tolerance_atr", "0"))),
                beyond_tolerance=str(planner_cfg.get("beyond_tolerance", "limit")))
            _rungs = [] if not _control.legs else LAD.plan_ladder(
                parsed, ladder_rows,
                max_tp_distance_pct=max_tp_pct if max_tp_pct > 0 else None,
                min_stop_distance=smap.min_stop_distance)
            if not _rungs:
                # Every row was dropped — the signal has no TP any row targets, or
                # the geometry puts each rung on the wrong side of its own stop.
                # Fall back to the ordinary fanout rather than place nothing.
                is_staged = False
                ladder_rows = None
                session.add(Event(kind="staged_fallback",
                                  payload={"signal_id": sig.id, "account_id": acct.id,
                                           "reason": "no_rung_the_signal_supports",
                                           "tps": len(parsed.tps or [])}))
                log.info("signal %s acct %s: ladder -> single-shot fallback (no usable rung)",
                         sig.id, acct.id)
            else:
                plan = FanoutPlan(symbol=parsed.symbol, direction=parsed.direction,
                                  order_type="LIMIT", legs=_rungs)
                session.add(Event(kind="staged_entry_decision", payload={
                    "signal_id": sig.id, "account_id": acct.id,
                    "current_price": str(current),
                    "mid": str(LAD.mid_level(parsed.entry_to, parsed.sl)),
                    "rungs": [{"when": l.tranche, "order": l.order_type,
                               "entry": str(l.entry), "tp_index": l.tp_index,
                               "trigger": str(l.trigger) if l.trigger is not None else None}
                              for l in plan.legs],
                    "cancel_on": LAD.cancel_rows(ladder_rows)}))
        if not is_staged:
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
            # `ts` is the instant the entry decision is being made, which is
            # what a `time_window` rule gates on (#214). Aware/UTC, so the leaf
            # can move it into the window's own zone.
            _filter_ctx = {"sessions": _active, "price": float(current),
                           "ts": datetime.now(timezone.utc)}
            # #132: graduate the adx_regime filter (#127) from shadow to LIVE. Build
            # the per-TF ADX ctx by computing it in the hot path (features are
            # captured post-execution, so they're not persisted yet). Only fetches
            # the timeframes an adx_regime rule references; fail-open on any miss.
            _adx_ctx = {}
            for _tf in ST.adx_rule_timeframes(_frules):
                _a = await _adx_read(adapter, smap.broker_epic, _tf)
                if _a is not None:
                    _adx_ctx[_tf] = _a
            if _adx_ctx:
                _filter_ctx["adx"] = _adx_ctx
            # #167: the generic TA ctx — any registry indicator on any timeframe,
            # resolved from the rule set itself. Same fail-open posture; still
            # nothing fetched when no rule references TA.
            _ta_block = await _ta_ctx(adapter, smap.broker_epic, _frules)
            if _ta_block:
                _filter_ctx["ta"] = _ta_block
            # #164: FAIL OPEN. An evaluator that raises used to propagate out of
            # handle_signal, where the consumer loop swallows it — AFTER the
            # signal is off the durable queue. That silently deleted the trade:
            # no order, no entry_filtered event, one stack trace. A filtration
            # rule must never be able to lose a signal, so any evaluator failure
            # now trades at full size and is recorded.
            try:
                _decision = ST.evaluate_filter_rules(_frules, _filter_ctx)
                _ff, _skip, _reasons = _decision.factor, _decision.skip, _decision.reasons
                _shadow, _evaluated = _decision.shadow, _decision.evaluated
            except Exception as exc:
                log.exception("signal %s acct %s: filtration evaluation FAILED — "
                              "failing open at full size: %s", sig.id, acct.id, exc)
                session.add(Event(kind="entry_filter_error", payload={
                    "signal_id": sig.id, "account_id": acct.id,
                    "error": str(exc), "rules": _frules}))
                _ff, _skip, _reasons, _shadow, _evaluated = 1.0, False, [], [], []
            # #167: what the shadow rules WOULD have done. Recorded before the live
            # skip returns, so a shadow rule is measurable on the very signals a
            # live rule rejects — otherwise the record is conditioned on the gate
            # we're trying to evaluate against.
            if _shadow:
                session.add(Event(kind="filter_shadow", payload={
                    "signal_id": sig.id, "account_id": acct.id, **_epoch,
                    "rules": _shadow,
                    # #213: the values, not just the verdict — a shadow rule that
                    # records only "matched" can never enter a feature screen.
                    "evaluated": [e for e in _evaluated if e["mode"] == "shadow"]}))
            if _skip:
                log.info("signal %s acct %s: SKIP by filtration (%s)", sig.id, acct.id, _reasons)
                session.add(Event(kind="entry_filtered", payload={
                    "signal_id": sig.id, "account_id": acct.id,
                    # `rules` stays a list of NAMES: every existing consumer of
                    # this event reads it that way. `evaluated` is the new audit
                    # trail — what each leaf asked and what it actually read
                    # (#213), without which a removal cannot be reconstructed.
                    **_epoch, "reason": "filtration_skip", "rules": _reasons,
                    "evaluated": _evaluated}))
                await session.commit()
                return
            filter_factor = Decimal(str(_ff))

        # Risk sizing (#84): per-(account, source) override -> [legacy source
        # risk_config] -> account risk_config. Risk relocated to Risk & Limits; the
        # source.risk_config fallback keeps sizing unchanged until it's cleared
        # (migrated into an override), so this deploy is non-breaking.
        _risk_override = (await session.execute(select(AccountSourceRisk).where(
            AccountSourceRisk.account_id == acct.id,
            AccountSourceRisk.source_id == sig.source_id))).scalar_one_or_none() \
            if sig.source_id else None
        risk = RiskConfig.from_dict(resolve_risk_config(
            (_risk_override.risk_config if _risk_override else None),
            (_risk_override.enabled if _risk_override else True),
            (source.risk_config if source and source.risk_config else None) or acct.risk_config))
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
                                       **_epoch,
                                       "reason": "counter_trend_desize", "aligned": False,
                                       "factor": str(trend_size_factor)}))
        if session_size_factor < 1:                     # session concentration de-size (#81)
            log.info("signal %s acct %s: de-sized session x%s",
                     sig.id, acct.id, session_size_factor)
            session.add(Event(kind="entry_filtered",
                              payload={"signal_id": sig.id, "account_id": acct.id,
                                       **_epoch,
                                       "reason": "session_desize",
                                       "factor": str(session_size_factor)}))
        if filter_factor != 1:                           # filtration scale (#84)
            log.info("signal %s acct %s: filtration scale x%s", sig.id, acct.id, filter_factor)
            session.add(Event(kind="entry_filtered",
                              payload={"signal_id": sig.id, "account_id": acct.id,
                                       **_epoch,
                                       "reason": "filtration_scale", "factor": str(filter_factor)}))
        instrument = InstrumentSpec(
            value_per_point=Decimal(str(smap.value_per_point)),
            min_lot=Decimal(str(smap.min_lot)),
            lot_step=Decimal(str(smap.lot_step)),
        )
        if ladder_rows is not None:
            # #250: every rung sized UP FRONT, against the risk the ordinary
            # fanout would have taken on this same signal. Fill every rung and run
            # to the stop and the loss is the same money the single-shot entry
            # would have lost — the ladder changes WHEN size arrives, never HOW
            # MUCH. Sizing a rung when it triggers would instead let a signal that
            # walks to MID and back stack risk without limit.
            _target = LAD.single_shot_risk(
                parsed, current_price=current, equity=equity, risk=risk,
                instrument=instrument, fx_factor=fx_factor,
                candle_high=candle_high, candle_low=candle_low,
                min_stop_distance=smap.min_stop_distance,
                max_tp_distance_pct=max_tp_pct if max_tp_pct > 0 else None,
                honor_market_hint=bool(planner_cfg.get("honor_market_hint", True)),
                chase_tolerance_r=Decimal(str(planner_cfg.get("chase_tolerance_r", "0.25"))),
                chase_tolerance_atr=Decimal(str(planner_cfg.get("chase_tolerance_atr", "0"))),
                beyond_tolerance=str(planner_cfg.get("beyond_tolerance", "limit")))
            _laddered = LAD.size_ladder(plan.legs, budget=_target,
                                        instrument=instrument, fx_factor=fx_factor)
            log.info("signal %s acct %s: ladder sized %s rungs to %s (single-shot %s)",
                     sig.id, acct.id, len(plan.legs), _laddered, _target)
            session.add(Event(kind="ladder_sized", payload={
                "signal_id": sig.id, "account_id": acct.id,
                "single_shot_risk": str(_target), "ladder_risk": str(_laddered),
                "rungs": len(plan.legs)}))
        else:
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

        # --- correlation-cluster risk budgeting (#106) — SHADOW-FIRST ---
        # Concurrent same-symbol/same-direction signals are usually the same market
        # view bet N times, so full-risk-each is N× concentration. Share ONE budget
        # across the cluster and de-size the arrival to fit (don't block — the
        # operator wants to keep trading, just not inflate risk linearly). Feature is
        # OFF unless the `risk_limits.cluster_risk` block is present; with enabled:false
        # it only COMPUTES + LOGS + TAGS (shadow), and only enabled:true changes lots.
        cluster_id = None
        cluster_alloc_rec = None
        _cluster_cfg = CL.merge_config((rl_cfg or {}).get("cluster_risk"))
        if _cluster_cfg:
            _since = utcnow() - timedelta(
                minutes=int(_cluster_cfg.get("window_minutes", 30) or 30))
            _open_rows = (await session.execute(select(
                Trade.direction, Trade.planned_risk, Trade.cluster_id, Trade.created_at
            ).where(Trade.account_id == acct.id, Trade.symbol == parsed.symbol,
                    Trade.status.in_(("open", "partial")),
                    Trade.created_at >= _since))).all()
            _same = [r for r in _open_rows if r.direction == parsed.direction]
            _opp = [r for r in _open_rows if r.direction != parsed.direction]
            # Inherit the earliest same-direction member's cluster_id, else start one.
            _cids = [r.cluster_id for r in sorted(_same, key=lambda r: r.created_at)
                     if r.cluster_id]
            cluster_id = _cids[0] if _cids else f"{parsed.symbol}:{parsed.direction}:{sig.id}"
            _budget = CL.resolve_budget(_cluster_cfg, rl_cfg.get("max_open_risk_per_symbol"))
            cluster_alloc_rec = CL.allocate(
                planned_risk,
                [CL.ClusterMember(planned_risk=Decimal(str(r.planned_risk or 0)))
                 for r in _same],
                budget=_budget, mode=_cluster_cfg.get("allocation", "equal"),
                decay=_cluster_cfg.get("decay", 0.5))
            cluster_alloc_rec["cluster_id"] = cluster_id
            cluster_alloc_rec["enforced"] = bool(_cluster_cfg.get("enabled"))
            _mixed = CL.mixed_exposure(
                parsed.direction, [r.planned_risk or 0 for r in _same],
                [r.planned_risk or 0 for r in _opp])
            if _mixed:
                cluster_alloc_rec["mixed"] = _mixed
            _scale = Decimal(cluster_alloc_rec["scale"])
            if _cluster_cfg.get("enabled") and _scale < 1:
                # ENFORCE: de-size the arrival to the shared budget (fit, don't block).
                _target = Decimal(cluster_alloc_rec["target_risk"])
                if _target <= 0:
                    for _l in plan.legs:
                        if _l.valid:
                            _l.valid = False
                            _l.skip_reason = "cluster budget exhausted"
                else:
                    cap_total_risk(plan.legs, cap=_target, instrument=instrument,
                                   fx_factor=fx_factor)
                planned_risk = plan_total_risk(plan.legs)
                cluster_alloc_rec["applied_risk"] = str(planned_risk)
                valid = plan.valid_legs
                if not valid:
                    session.add(Event(kind="cluster_desized", payload={
                        "signal_id": sig.id, "account_id": acct.id,
                        "cluster_id": cluster_id, "alloc": cluster_alloc_rec,
                        "result": "rejected_below_min_lot"}))
                    await session.commit()
                    log.warning("signal %s acct %s: cluster budget exhausted — no legs "
                                "above min_lot (cluster %s)", sig.id, acct.id, cluster_id)
                    return
                session.add(Event(kind="cluster_desized", payload={
                    "signal_id": sig.id, "account_id": acct.id,
                    "cluster_id": cluster_id, "alloc": cluster_alloc_rec}))
                log.warning("signal %s acct %s: cluster de-size x%s -> risk %s "
                            "(cluster %s, N=%s)", sig.id, acct.id, _scale, planned_risk,
                            cluster_id, cluster_alloc_rec["cluster_size"])
            else:
                # SHADOW: record what we WOULD do; lots unchanged (measure-before-gate).
                session.add(Event(kind="cluster_shadow", payload={
                    "signal_id": sig.id, "account_id": acct.id,
                    "cluster_id": cluster_id, "alloc": cluster_alloc_rec}))
                if _scale < 1:
                    log.info("signal %s acct %s: cluster SHADOW would de-size x%s "
                             "(cluster %s, N=%s)", sig.id, acct.id, _scale, cluster_id,
                             cluster_alloc_rec["cluster_size"])

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
                                           "planned_risk": str(planned_risk),
                                           # #202: the breaker states its BASIS. A
                                           # halt is only reviewable if the number
                                           # it fired on names where it came from.
                                           "day_realized": str(day_realized),
                                           "pl_basis": "trades.realized_pl",
                                           "reason": reason}))
                await session.commit()
                log.warning("signal %s acct %s: RISK-LIMIT BLOCK — %s",
                            sig.id, acct.id, reason)
                return

            # --- Graduated soft-loss cooldown (#126) — ADDITIVE to the hard halt
            # above. Inert unless risk_limits.daily_soft_loss_limit is set (default
            # 0). Stateless mode: pause NEW entries while the day's realized sits in
            # the soft band (existing positions keep managing), auto-resuming when
            # closed winners recover it. The timed-window variant (breaker_cooldown_
            # minutes) is implemented+tested in guard.soft_breaker_decision but needs
            # a persisted per-account cooldown_until to fire live — a follow-up.
            # Same cfg + basis on every A/B account -> symmetric by construction.
            # BASIS (#74/#126/#202): `day_realized` is Σ `trades.realized_pl`, and
            # that is the RIGHT number — do not "upgrade" it to the activity
            # ledger. The note that used to stand here said the ledger overstates
            # the loss and told the next reader to feed broker truth instead; #202
            # showed that backwards. `position_activities.realized_pl` stored a
            # ratcheted stop-out UNSIGNED, i.e. a loss recorded as a gain, so the
            # activity ledger UNDERSTATES losses — pointing a loss breaker at it
            # would blind the one guard whose entire job is to see them.
            _bd = soft_breaker_decision(day_realized=day_realized, cfg=rl_cfg,
                                        now=utcnow(), cooldown_until=None)
            if _bd["block"]:
                session.add(Event(kind="breaker_state",
                                  payload={"signal_id": sig.id, "account_id": acct.id,
                                           "state": _bd["state"], "day_realized": str(day_realized),
                                           "pl_basis": "trades.realized_pl",
                                           "reason": _bd["reason"]}))
                await session.commit()
                log.warning("signal %s acct %s: SOFT-BREAKER %s — %s",
                            sig.id, acct.id, _bd["state"], _bd["reason"])
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
            _chain,
            source_rules=(source.strategy or {}).get("sl_rules") if source else None,
            global_default=_gstrat.get("default_sl_rules"))
        # Attribute to the row that actually PROVIDED the exit rules — with the
        # #104 pillar cascade that may be a less-specific row than chain[0].
        _exit_row = next((s for s in _chain if (s.exit_policy or {}).get("sl_rules")), None)
        _strategy_id = _exit_row.id if _exit_row else None

        trade = Trade(signal_id=sig.id, account_id=acct.id, symbol=parsed.symbol,
                      direction=parsed.direction, status="open",
                      planned_risk=planned_risk,
                      sl_rules=_sl_rules, strategy_id=_strategy_id,
                      cluster_id=cluster_id, cluster_alloc=cluster_alloc_rec,
                      # How late in the fanout this arm was reached (#211), so
                      # the handicap is measurable instead of invisible.
                      placement_lag_ms=placement_lag_ms,
                      # As RUN, not as configured (#156): a staged strategy that
                      # fell back to the single-shot planner is a CONTROL trade and
                      # must not be counted in the staged arm.
                      entry_style="staged" if is_staged else "single_shot")
        session.add(trade)
        await session.flush()
        if _strategy_id:
            log.info("signal %s acct %s: strategy #%s (%s) applied", sig.id, acct.id,
                     _strategy_id, _exit_row.label or "override")

        # Broker-enforced expiry for any working (LIMIT/STOP) leg (#40): the entry
        # TTL from the resolved entry policy (planner_cfg carries ttl_minutes from
        # the strategy / source / global), clamped to a safe range so an unfilled
        # entry can't rest as GTC and fill hours late at a stale price.
        good_till = utcnow() + timedelta(
            minutes=effective_entry_ttl_min({"entry_ttl_minutes": planner_cfg.get("ttl_minutes")}))

        placed = 0
        placed_lots = Decimal("0")     # size that actually reached the broker (#179)
        tranche_legs = {}          # ladder trigger -> the Leg ids it owns (#250)
        for pleg in valid:
            leg = Leg(trade_id=trade.id, tp_index=pleg.tp_index,
                      order_type=pleg.order_type, entry=pleg.entry, tp=pleg.tp,
                      sl=pleg.sl, lot=pleg.lot, status="pending")
            session.add(leg)
            await session.flush()
            if getattr(pleg, "tranche", None):
                tranche_legs.setdefault(pleg.tranche, []).append(leg.id)
            # A rung that waits for a trigger is NOT sent to the broker now (#250):
            # the monitor places it when price reaches its level. Persist as
            # 'staged' with the level/mode on the Leg row so the monitor can place
            # it later without re-planning. Only `signal` rows go out immediately.
            if (getattr(pleg, "tranche", None)
                    and pleg.tranche != LAD.WHEN_SIGNAL):
                leg.status = "staged"
                session.add(Event(trade_id=trade.id, leg_id=leg.id, kind="staged_leg",
                                  payload={"tranche": pleg.tranche, "mode": pleg.order_type,
                                           "entry": str(pleg.entry),
                                           "trigger": str(pleg.trigger) if pleg.trigger is not None else None}))
                continue
            # #221: place, and RECOVER a refusal we understand rather than
            # dropping the leg's size. Two broker errors are routinely
            # recoverable — a LIMIT the market reached before we submitted it,
            # and a take-profit outside the broker's band (the error names the
            # bound). Both were discarding the leg: 49 legs / 954.93 lots over
            # six weeks, invisible because only a `rejected` row was written and
            # nothing recorded the exposure the trade therefore never carried.
            # The decision is pure and lives in execution/placement.py; at most
            # ONE retry, and anything unrecognised still fails closed.
            _otype, _tp, _retried_as, _err, res = pleg.order_type, pleg.tp, None, None, None
            _first_err = None
            for _attempt in (1, 2):
                try:
                    _is_market = _otype == "MARKET"
                    res = await adapter.place_order(PlaceOrderRequest(
                        broker_symbol=smap.broker_epic,
                        side=OrderSide.BUY if side_buy else OrderSide.SELL,
                        order_type=OrderType.MARKET if _is_market else OrderType.LIMIT,
                        quantity=pleg.lot,
                        limit_price=None if _is_market else pleg.entry,
                        stop_loss=pleg.sl, take_profit=_tp,
                        # Broker-enforced TTL for working orders (#40) — never GTC.
                        good_till=None if _is_market else good_till,
                    ))
                    _err = res.rejection_reason if res.status == OrderStatus.REJECTED else None
                except Exception as exc:      # one leg failing must not sink the rest
                    res, _err = None, exc
                if _err is None or _attempt == 2:
                    break
                _first_err = _err          # what the broker said the FIRST time
                _plan = PLACE.retry_plan(_err, side_buy=side_buy, order_type=_otype,
                                         entry=pleg.entry, take_profit=_tp)
                if not _plan:
                    break
                if _plan["action"] == PLACE.RETRY_AS_MARKET:
                    # Refused as "at market" means price already reached the
                    # level, so a market fill is at-or-BETTER in both directions
                    # and planned risk cannot rise (#140's argument, one module).
                    _otype, _retried_as = "MARKET", "MARKET"
                else:
                    _tp, _retried_as = _plan["take_profit"], "TP@%s" % _plan["take_profit"]
                log.warning("signal %s acct %s leg %s refused (%s) — retrying as %s",
                            sig.id, acct.id, leg.id, str(_err)[:120], _retried_as)

            if _err is not None:
                # Unrecoverable. Make it visible instead of silently leaving the
                # leg 'pending' — and say how much size never reached the broker.
                leg.status = "rejected"
                leg.outcome = "rejected"
                session.add(Event(trade_id=trade.id, leg_id=leg.id, kind="reject",
                                  payload={"ref": getattr(res, "broker_order_ref", None),
                                           "reason": str(_err)[:300]}))
                session.add(Event(trade_id=trade.id, leg_id=leg.id, kind="leg_rejected",
                                  payload=PLACE.rejection_event(
                                      leg, intended_lot=pleg.lot, error=_err,
                                      retried_as=_retried_as, recovered=False)))
                log.warning("signal %s acct %s leg %s REJECTED by broker: %s",
                            sig.id, acct.id, leg.id, str(_err)[:200])
                _broker_error(
                    "reject", f"Order rejected: {str(_err) or 'no reason given'}",
                    account=acct.name, symbol=sig.symbol, account_id=acct.id)
            else:
                # Recording the accepted order must not be able to sink the rest
                # of the ladder either — the guarantee the old `except Exception`
                # around this whole block used to give.
                try:
                    if _retried_as is not None:
                        # The leg is not what the planner asked for; persist what
                        # was actually sent so reconciliation reads the truth, and
                        # record the recovery with the size it saved.
                        leg.order_type = _otype
                        leg.tp = _tp
                        session.add(Event(trade_id=trade.id, leg_id=leg.id,
                                          kind="leg_rejected",
                                          payload=PLACE.rejection_event(
                                              leg, intended_lot=pleg.lot, error=_first_err,
                                              retried_as=_retried_as, recovered=True)))
                    if _otype == "MARKET":
                        leg.broker_position_ref = res.broker_order_ref
                        leg.status = "open" if res.status == OrderStatus.FILLED else "pending"
                        # A 0 fill level is an UNKNOWN fill, not a fill at zero.
                        # Persist NULL so the entry/R basis falls back to
                        # leg.entry (the monitor backfills from the live position
                        # next tick); a stored 0 killed the SL ratchet (#159).
                        leg.fill_price = res.fill_price or None
                        if leg.fill_price is None and res.status == OrderStatus.FILLED:
                            session.add(Event(trade_id=trade.id, leg_id=leg.id,
                                              kind="fill_price_unknown",
                                              payload={"ref": res.broker_order_ref}))
                    else:
                        leg.broker_order_ref = res.broker_order_ref
                        leg.status = "working"
                    session.add(Event(trade_id=trade.id, leg_id=leg.id, kind="placed",
                                      payload={"ref": res.broker_order_ref,
                                               "status": res.status.value}))
                    placed += 1
                    placed_lots += pleg.lot
                except Exception as exc:      # one leg failing must not sink the rest
                    leg.status = "rejected"
                    session.add(Event(trade_id=trade.id, leg_id=leg.id, kind="reject",
                                      payload={"error": str(exc)[:300]}))
                    log.warning("leg record failed (trade %s): %s", trade.id, exc)
            await asyncio.sleep(1.0 / max(settings.broker_rate_per_sec, 0.1))

        # Persist the staged-entry state (#129) so the monitor can drive the DECIDE
        # engine each tick: the geometry + frozen config + one tranche row per role
        # (toe-in already deployed; runner/reclaim pending, with their own TTL clock).
        if ladder_rows is not None:
            _mid = LAD.mid_level(parsed.entry_to, parsed.sl)
            session.add(StagedEntry(
                trade_id=trade.id, account_id=acct.id, direction=parsed.direction,
                near_edge=parsed.entry_from, deep_edge=parsed.entry_to,
                sl=parsed.sl, atr=None,
                max_adverse_beyond_deep=Decimal("0"),
                cfg={"ladder": ladder_rows,
                     **STG.staged_config(planner_cfg.get("staged"))}))
            for _when, _ids in tranche_legs.items():
                if not _ids:
                    continue
                _pl = next((p for p in valid if getattr(p, "tranche", None) == _when), None)
                session.add(StagedTranche(
                    trade_id=trade.id, role=_when,
                    state="deployed" if _when == LAD.WHEN_SIGNAL else "pending",
                    mode=(_pl.order_type if _pl else None),
                    trigger_level=(_pl.trigger if _pl and _pl.trigger is not None else None),
                    leg_ids=_ids))
            # A `cancel everything else` row owns no legs, so it needs a row of its
            # own or the monitor has nothing to notice. Its trigger level is the
            # price that fires it — TP1 today, resolved here where the TPs are known.
            for _when in LAD.cancel_rows(ladder_rows):
                _lvl = LAD.trigger_level(_when, parsed)
                if _lvl is None:
                    continue
                session.add(StagedTranche(
                    trade_id=trade.id, role=f"cancel:{_when}", state="pending",
                    mode="CANCEL", trigger_level=Decimal(str(_lvl)), leg_ids=[]))
            log.info("signal %s acct %s: LADDER entry (%s rungs across %s triggers)",
                     sig.id, acct.id, sum(len(v) for v in tranche_legs.values()),
                     len(tranche_legs))

        await session.commit()
        await bus.publish(CH_TRADE_OPENED, {"trade_id": trade.id, "account_id": acct.id,
                                            "placed": placed})
        if placed:
            _notify("order_placed", {
                "symbol": sig.symbol, "direction": sig.direction, "account": acct.name,
                "size": str(placed_lots),
                "channel": source.name if source else None,
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
