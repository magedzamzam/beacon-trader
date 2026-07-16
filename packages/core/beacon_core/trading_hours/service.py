"""Trading Hours orchestration: config, aggregate status, and calendar refresh.

The status is READ-ONLY intelligence for now — it tells you the session, the
news blackout, and the holiday/weekend state so you can see them and build trade
gating on top later. Nothing here blocks trading yet.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db.base import Session
from ..db.models import EconEvent
from ..logging import get_logger
from ..settings_store import get_setting, set_setting
from ..tasks import spawn_bg
from ..timeutil import utcnow, parse_iso_utc
from . import calendar, holidays, sessions
from .config import DEFAULT_CONFIG, sanitize_config

log = get_logger("trading_hours")

SETTING_KEY = "trading_hours"
_REFRESH_TS_KEY = "econ_refreshed_at"
_REFRESH_STALE_SECONDS = 6 * 3600


async def load_config(session) -> dict:
    stored = await get_setting(session, SETTING_KEY, None)
    return sanitize_config(stored) if stored else sanitize_config(None)


async def save_config(session, cfg: dict) -> dict:
    clean = sanitize_config(cfg)
    await set_setting(session, SETTING_KEY, clean)
    return clean


async def _refresh_bg() -> None:
    try:
        events = await calendar.fetch_events()
        if not events:
            return
        rows = [{"ts": e["ts"], "ccy": e.get("ccy"), "impact": e.get("impact"),
                 "title": (e.get("title") or "")[:256]} for e in events if e.get("ts")]
        if not rows:
            return
        async with Session()() as s:
            await s.execute(pg_insert(EconEvent).values(rows)
                            .on_conflict_do_nothing(constraint="uq_econ_event"))
            await s.commit()
        log.info("econ calendar refreshed: %s events", len(rows))
    except Exception as exc:
        log.warning("calendar refresh failed: %s", exc)


async def maybe_refresh(session) -> None:
    """Refresh the persisted calendar in the background if it's stale — never
    blocks the caller. Optimistically stamps the refresh time so concurrent
    callers don't all fire."""
    last = await get_setting(session, _REFRESH_TS_KEY, None)
    parsed = parse_iso_utc(last)
    stale = parsed is None or (utcnow() - parsed).total_seconds() > _REFRESH_STALE_SECONDS
    if not stale:
        return
    await set_setting(session, _REFRESH_TS_KEY, utcnow().isoformat())
    spawn_bg(_refresh_bg())


async def refresh_now(session) -> int:
    """Force a synchronous refresh (for the manual button). Returns event count."""
    events = await calendar.fetch_events()
    rows = [{"ts": e["ts"], "ccy": e.get("ccy"), "impact": e.get("impact"),
             "title": (e.get("title") or "")[:256]} for e in events if e.get("ts")]
    if rows:
        await session.execute(pg_insert(EconEvent).values(rows)
                              .on_conflict_do_nothing(constraint="uq_econ_event"))
        await set_setting(session, _REFRESH_TS_KEY, utcnow().isoformat())
        await session.commit()
    return len(rows)


async def _load_events(session, now):
    lo, hi = now - dt.timedelta(days=1), now + dt.timedelta(days=8)
    rows = (await session.execute(select(EconEvent)
            .where(EconEvent.ts >= lo, EconEvent.ts <= hi)
            .order_by(EconEvent.ts))).scalars().all()
    return [{"ts": r.ts, "ccy": r.ccy, "impact": r.impact, "title": r.title} for r in rows]


def _blackout(events, now, news_cfg: dict) -> dict:
    """Tiered news-blackout status from a resolved `news` config (#77)."""
    return calendar.blackout_status(
        events, now, impacts=news_cfg.get("impacts", ["high"]),
        before_min=news_cfg.get("before_min", 3), after_min=news_cfg.get("after_min", 3),
        currencies=news_cfg.get("currencies") or None,
        major_before_min=news_cfg.get("major_before_min"),
        major_after_min=news_cfg.get("major_after_min"),
        major_keywords=news_cfg.get("major_keywords"))


async def entry_blackout(session, now: Optional[dt.datetime] = None) -> Optional[dict]:
    """The active news-blackout window blocking NEW entries right now, or None
    (#77). Reads the trading_hours config + persisted econ events. Gates only when
    news.enabled AND news.gate_entries. Does NOT touch open positions. Fail-open:
    any error / disabled / no active window -> None (never blocks on a glitch)."""
    try:
        cfg = await load_config(session)
        news = cfg.get("news", {})
        if not news.get("enabled", True) or not news.get("gate_entries", True):
            return None
        now = now or utcnow()
        await maybe_refresh(session)
        events = await _load_events(session, now)
        st = _blackout(events, now, news)
        return st["active"] if st.get("in_blackout") else None
    except Exception as exc:
        log.warning("news entry gate failed (fail-open, not blocking): %s", exc)
        return None


async def status(session) -> dict:
    cfg = await load_config(session)
    now = utcnow()
    sess = sessions.status(cfg["sessions"], now)

    news_cfg = cfg["news"]
    if news_cfg.get("enabled", True):
        await maybe_refresh(session)
        events = await _load_events(session, now)
        news = _blackout(events, now, news_cfg)
    else:
        news = {"in_blackout": False, "active": None, "next": None}

    hol = holidays.status(now)
    return {"now_utc": now.isoformat(), "sessions": sess, "news": news,
            "holiday": hol, "config": cfg}
