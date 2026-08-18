"""Capture a signal-time TA snapshot across the configured timeframes and
indicators, and persist it. Best-effort: any timeframe (or the whole capture)
that fails just logs and is skipped — never affects trading. Called off the hot
path (after orders are placed) so it adds no execution latency.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..analysis import sidecar
from ..db.models import ExecutionStrategy, SignalFeature
from ..execution import strategy as ST
from ..logging import get_logger
from ..settings_store import get_setting
from ..timeutil import utcnow
from .features import compute_timeframe
from .registry import DEFAULT_CONFIG, TF_RESOLUTION, sanitize_config

log = get_logger("ta.capture")

TA_SETTING_KEY = "ta"
MAX_BARS = 250


def _session_tag(ts: dt.datetime) -> str:
    h = ts.astimezone(dt.timezone.utc).hour if ts.tzinfo else ts.hour
    if h < 7:
        return "ASIA"
    if h < 12:
        return "LONDON"
    if h < 16:
        return "OVERLAP"
    if h < 21:
        return "NY"
    return "LATE"


async def load_config(session) -> dict:
    stored = await get_setting(session, TA_SETTING_KEY, None)
    return sanitize_config(stored) if stored else dict(DEFAULT_CONFIG)


async def live_rule_requirements(session) -> list:
    """Every `(indicator, timeframe)` any ENABLED execution strategy references,
    as `ta_rule_requirements` rows. Best-effort: a failure here degrades capture
    to the fixed configured set, never to no capture at all."""
    from sqlalchemy import select
    try:
        rows = (await session.execute(
            select(ExecutionStrategy.entry_filters).where(
                ExecutionStrategy.enabled.is_(True)))).scalars().all()
    except Exception as exc:                     # pragma: no cover - degraded path
        log.warning("could not read live filter rules for capture: %s", exc)
        return []
    reqs, seen = [], set()
    for ef in rows:
        for req in ST.ta_rule_requirements((ef or {}).get("rules") or []):
            token = (req["timeframe"], req["key"])
            if token not in seen:
                seen.add(token)
                reqs.append(req)
    return reqs


def capture_plan(cfg: dict, requirements) -> dict:
    """`{timeframe: [{id, params}, ...]}` — the fixed configured capture UNIONED
    with everything a live rule references (#213).

    CAPTURE FOLLOWS CONFIGURATION. Since #167 an `indicator` gate can name any
    registry entry, which made arming one a config act; the capture list stayed a
    hand-maintained constant. So the highest-volume rule in the experiment gated
    51 removals on `cci`, which `signal_features` has never held a single value
    of — no counterfactual on the other twelve channels, no out-of-sample screen,
    nothing to review. A rule can now never reference something we do not
    persist, because the rules themselves are an input to what gets persisted."""
    timeframes = list(cfg.get("timeframes") or DEFAULT_CONFIG["timeframes"])
    base = list(cfg.get("indicators") or DEFAULT_CONFIG["indicators"])
    plan = {tf: list(base) for tf in timeframes}
    for req in requirements or ():
        tf = req.get("timeframe")
        if not tf or tf not in TF_RESOLUTION:
            continue                             # nothing can fetch bars for it
        want = {"id": req["id"], "params": req.get("params") or {}}
        bucket = plan.setdefault(tf, list(base))
        if not any(i.get("id") == want["id"]
                   and (i.get("params") or {}) == want["params"] for i in bucket):
            bucket.append(want)
    return plan


async def capture_for_signal(session, sig, adapter, smap, *, max_bars: int = MAX_BARS):
    """Fetch bars for each configured timeframe, compute the configured
    indicators, and upsert one SignalFeature row for `sig`."""
    cfg = await load_config(session)
    plan = capture_plan(cfg, await live_rule_requirements(session))
    timeframes = sorted(plan, key=lambda t: list(TF_RESOLUTION).index(t))
    n_indicators = len({(i["id"], tuple(sorted((i.get("params") or {}).items())))
                        for items in plan.values() for i in items})

    # Shadow analytics sidecar (#51/#52): reuse the bars we're about to fetch for
    # its primary timeframe (no extra broker call), so estimators run on the same
    # window. Loaded up front so we know which timeframe's bars to retain.
    try:
        a_cfg = await sidecar.load_config(session)
    except Exception:
        a_cfg = {"enabled": False}
    a_tf = a_cfg.get("timeframe", "1h")
    analytics_bars = None

    # Reference price (live mid) for above/below + distance features.
    price = None
    try:
        q = await adapter.get_quote(smap.broker_epic)
        if getattr(q, "bid", None) is not None and getattr(q, "offer", None) is not None:
            price = (float(q.bid) + float(q.offer)) / 2.0
    except Exception as exc:
        log.info("quote for TA capture failed (%s): %s", smap.broker_epic, exc)

    tf_features: dict = {}
    for label in timeframes:
        resolution = TF_RESOLUTION.get(label)
        if not resolution:
            continue
        try:
            bars = await adapter.get_bars(smap.broker_epic, resolution, max_bars=max_bars)
        except Exception as exc:
            log.info("bars %s/%s failed: %s", smap.broker_epic, resolution, exc)
            continue
        if label == a_tf:                        # retain for the analytics sidecar
            analytics_bars = bars
        f = compute_timeframe(bars, price, plan[label])
        if f is not None:
            tf_features[label] = f

    if not tf_features:
        log.info("no TA features computed for signal %s", sig.id)
        return None

    now = utcnow()
    stmt = pg_insert(SignalFeature).values(
        signal_id=sig.id, symbol=sig.symbol, direction=sig.direction,
        price=Decimal(str(price)) if price is not None else None,
        session=_session_tag(now), utc_hour=now.hour,
        features=tf_features, captured_at=now,
    ).on_conflict_do_nothing(constraint="uq_signal_feature")
    await session.execute(stmt)
    log.info("captured TA features for signal %s (%s timeframes, %s indicators)",
             sig.id, len(tf_features), n_indicators)

    # Shadow analytics sidecar (#51/#52) — runs in its OWN session, fully
    # isolated; any failure is swallowed here and never affects TA capture (which
    # itself never affects trading).
    if a_cfg.get("enabled"):
        try:
            await sidecar.capture_analytics(
                signal_id=sig.id, symbol=sig.symbol, direction=sig.direction,
                source_id=getattr(sig, "source_id", None), features=tf_features,
                bars=analytics_bars, price=price, timeframe=a_tf, cfg=a_cfg,
                sl=getattr(sig, "sl", None), entry_from=getattr(sig, "entry_from", None),
                entry_to=getattr(sig, "entry_to", None), tps=getattr(sig, "tps", None))
        except Exception as exc:
            log.warning("ANALYTICS-SIDECAR-DEGRADED: capture failed (signal %s): %s",
                        sig.id, exc)

    return tf_features
