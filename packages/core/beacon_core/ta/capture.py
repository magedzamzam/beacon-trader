"""Capture a signal-time TA snapshot across timeframes and persist it.

Best-effort: any timeframe (or the whole capture) that fails just logs and is
skipped — this never affects trading. Called off the hot path (after orders are
placed) so it adds no execution latency.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db.models import SignalFeature
from ..logging import get_logger
from .features import timeframe_features

log = get_logger("ta.capture")

# (label stored in JSON, broker resolution). Matches Capital.com get_bars.
TIMEFRAMES = [
    ("1m", "MINUTE"), ("5m", "MINUTE_5"), ("15m", "MINUTE_15"),
    ("30m", "MINUTE_30"), ("1h", "HOUR"), ("4h", "HOUR_4"), ("1d", "DAY"),
]
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


async def capture_for_signal(session, sig, adapter, smap, *, max_bars: int = MAX_BARS):
    """Fetch bars per timeframe from `adapter`, compute features, upsert one
    SignalFeature row for `sig`. Returns the features dict (or None)."""
    # Reference price (live mid) for above/below + distance features.
    price = None
    try:
        q = await adapter.get_quote(smap.broker_epic)
        if getattr(q, "bid", None) is not None and getattr(q, "offer", None) is not None:
            price = (float(q.bid) + float(q.offer)) / 2.0
    except Exception as exc:
        log.info("quote for TA capture failed (%s): %s", smap.broker_epic, exc)

    tf_features: dict = {}
    for label, resolution in TIMEFRAMES:
        try:
            bars = await adapter.get_bars(smap.broker_epic, resolution, max_bars=max_bars)
        except Exception as exc:
            log.info("bars %s/%s failed: %s", smap.broker_epic, resolution, exc)
            continue
        f = timeframe_features(bars, price)
        if f is not None:
            tf_features[label] = f

    if not tf_features:
        log.info("no TA features computed for signal %s", sig.id)
        return None

    now = dt.datetime.now(dt.timezone.utc)
    stmt = pg_insert(SignalFeature).values(
        signal_id=sig.id, symbol=sig.symbol, direction=sig.direction,
        price=Decimal(str(price)) if price is not None else None,
        session=_session_tag(now), utc_hour=now.hour,
        features=tf_features, captured_at=now,
    ).on_conflict_do_nothing(constraint="uq_signal_feature")
    await session.execute(stmt)
    log.info("captured TA features for signal %s (%s timeframes)", sig.id, len(tf_features))
    return tf_features
