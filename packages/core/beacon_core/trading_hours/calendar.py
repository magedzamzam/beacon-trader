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

log = get_logger("trading_hours.calendar")

DEFAULT_URL = os.getenv("TRADING_HOURS_CALENDAR_URL",
                        "https://nfs.faireconomy.media/ff_calendar_thisweek.json")


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
                    currencies: Optional[list] = None) -> dict:
    """Are we inside a high-impact news window? `events` carry tz-aware `ts`."""
    impacts = tuple(i.lower() for i in (impacts or ("high",)))
    ccys = set(c.upper() for c in currencies) if currencies else None

    def _relevant(e):
        if (e.get("impact") or "").lower() not in impacts:
            return False
        if ccys and (e.get("ccy") or "").upper() not in ccys:
            return False
        return True

    active, nxt = None, None
    for e in events:
        if not _relevant(e) or not e.get("ts"):
            continue
        start = e["ts"] - dt.timedelta(minutes=before_min)
        end = e["ts"] + dt.timedelta(minutes=after_min)
        if start <= now <= end:
            active = e
        if e["ts"] > now and (nxt is None or e["ts"] < nxt["ts"]):
            nxt = e

    def _shape(e):
        if not e:
            return None
        return {"title": e.get("title"), "ccy": e.get("ccy"), "impact": e.get("impact"),
                "ts": e["ts"].isoformat(),
                "in_min": max(0, int((e["ts"] - now).total_seconds() // 60))}

    return {"in_blackout": active is not None, "active": _shape(active), "next": _shape(nxt)}
