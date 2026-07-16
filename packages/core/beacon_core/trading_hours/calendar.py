"""Economic-calendar feed for the news blackout — isolated + swappable.

Default source: the ForexFactory weekly JSON mirror (free, no key). Override
with TRADING_HOURS_CALENDAR_URL. `fetch_events` normalizes to
{ts (UTC), ccy, impact, title}; `blackout_status` derives the current window.
httpx is imported lazily so `blackout_status` unit-tests without the net stack.
"""
from __future__ import annotations

import datetime as dt
import os
from typing import List, Optional

from ..logging import get_logger
from ..timeutil import parse_iso_utc as _parse_ts   # UTC-normalizing ISO parse (#41)

log = get_logger("trading_hours.calendar")

DEFAULT_URL = os.getenv("TRADING_HOURS_CALENDAR_URL",
                        "https://nfs.faireconomy.media/ff_calendar_thisweek.json")


async def fetch_events(url: Optional[str] = None) -> List[dict]:
    """Normalized upcoming events, or [] on any failure (fail-safe)."""
    import httpx
    url = url or DEFAULT_URL
    try:
        async with httpx.AsyncClient(timeout=20.0,
                                     headers={"User-Agent": "beacon-trader/1.0"}) as c:
            resp = await c.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.warning("calendar fetch failed: %s", exc)
        return []
    if not isinstance(data, list):
        return []
    out = []
    for e in data:
        ts = _parse_ts(e.get("date"))
        if ts is None:
            continue
        out.append({"ts": ts,
                    "ccy": e.get("country") or e.get("currency"),
                    "impact": (e.get("impact") or "").strip().lower() or None,
                    "title": e.get("title") or e.get("event")})
    return out


def blackout_status(events: List[dict], now: dt.datetime, *, impacts=("high",),
                    before_min: int = 3, after_min: int = 3,
                    currencies: Optional[list] = None,
                    major_before_min: Optional[int] = None,
                    major_after_min: Optional[int] = None,
                    major_keywords: Optional[list] = None) -> dict:
    """Are we inside a high-impact news window? `events` carry tz-aware `ts`.

    Tiered (#77): an event whose title matches one of `major_keywords` (CPI/NFP/
    FOMC-grade) uses the wider `major_before_min`/`major_after_min` window instead
    of the default ±`before_min`/`after_min`. When the major_* args are omitted,
    behaviour is identical to the original single-tier window (backward-compatible)."""
    impacts = tuple(i.lower() for i in (impacts or ("high",)))
    ccys = set(c.upper() for c in currencies) if currencies else None
    kw = tuple(k.lower() for k in (major_keywords or ()) if k)

    def _relevant(e):
        if (e.get("impact") or "").lower() not in impacts:
            return False
        if ccys and (e.get("ccy") or "").upper() not in ccys:
            return False
        return True

    def _is_major(e):
        title = (e.get("title") or "").lower()
        return bool(kw) and any(k in title for k in kw)

    def _window(major):
        b = major_before_min if (major and major_before_min is not None) else before_min
        a = major_after_min if (major and major_after_min is not None) else after_min
        return b, a

    active, nxt = None, None
    for e in events:
        if not _relevant(e) or not e.get("ts"):
            continue
        major = _is_major(e)
        b, a = _window(major)
        start = e["ts"] - dt.timedelta(minutes=b)
        end = e["ts"] + dt.timedelta(minutes=a)
        if start <= now <= end:
            # A major window wins over a standard one if both are active.
            if active is None or (major and not active[1]):
                active = (e, major)
        if e["ts"] > now and (nxt is None or e["ts"] < nxt["ts"]):
            nxt = e

    def _shape(pair):
        if not pair:
            return None
        e, major = pair if isinstance(pair, tuple) else (pair, _is_major(pair))
        b, a = _window(major)
        return {"title": e.get("title"), "ccy": e.get("ccy"), "impact": e.get("impact"),
                "ts": e["ts"].isoformat(), "tier": "major" if major else "standard",
                "before_min": b, "after_min": a,
                "in_min": max(0, int((e["ts"] - now).total_seconds() // 60))}

    return {"in_blackout": active is not None, "active": _shape(active),
            "next": _shape(nxt)}
