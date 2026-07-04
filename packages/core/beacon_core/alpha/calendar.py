"""Economic calendar feed — isolated + swappable.

Default source is the ForexFactory weekly JSON mirror (free, no key), which
returns entries shaped like:
    {"title": "...", "country": "USD", "date": "2026-07-07T12:30:00-04:00",
     "impact": "High", "forecast": "...", "previous": "..."}

`fetch_events` normalizes to {ts (UTC datetime), ccy, impact (lowercased),
title}. To swap providers, set ECON_CALENDAR_URL or replace this module's
`fetch_events` — nothing else in the codebase parses the raw feed.
"""
from __future__ import annotations

import datetime as dt
from typing import List, Optional

from ..config import get_settings
from ..logging import get_logger

log = get_logger("alpha.calendar")

_HIGH = {"high", "red"}


def _parse_ts(v) -> Optional[dt.datetime]:
    if not v:
        return None
    try:
        d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except (ValueError, AttributeError):
        return None


async def fetch_events(url: Optional[str] = None) -> List[dict]:
    """Return normalized upcoming events, or [] on any failure (fail-safe)."""
    import httpx
    url = url or get_settings().econ_calendar_url
    try:
        async with httpx.AsyncClient(timeout=20.0,
                                     headers={"User-Agent": "beacon-trader/1.0"}) as c:
            resp = await c.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:                     # network/parse — never crash caller
        log.warning("econ calendar fetch failed: %s", exc)
        return []

    if not isinstance(data, list):
        log.warning("econ calendar: unexpected shape %s", type(data).__name__)
        return []

    out: List[dict] = []
    for e in data:
        ts = _parse_ts(e.get("date"))
        if ts is None:
            continue
        out.append({
            "ts": ts,
            "ccy": (e.get("country") or e.get("currency") or None),
            "impact": (e.get("impact") or "").strip().lower() or None,
            "title": e.get("title") or e.get("event") or None,
        })
    return out


def is_high_impact(impact: Optional[str]) -> bool:
    return (impact or "").strip().lower() in _HIGH
