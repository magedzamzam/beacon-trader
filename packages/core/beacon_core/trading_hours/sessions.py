"""Trading-session windows, computed live in each market's local timezone so
DST is handled automatically. Windows are configurable; these are the defaults.
"""
from __future__ import annotations

import datetime as dt
from typing import List
from zoneinfo import ZoneInfo

# Default session windows in each market's LOCAL time (DST-aware via tz).
DEFAULT_SESSIONS = [
    {"id": "asian", "label": "Asian (Tokyo)", "tz": "Asia/Tokyo",
     "start": "09:00", "end": "18:00", "enabled": True},
    {"id": "london", "label": "London", "tz": "Europe/London",
     "start": "08:00", "end": "17:00", "enabled": True},
    {"id": "newyork", "label": "New York", "tz": "America/New_York",
     "start": "08:00", "end": "17:00", "enabled": True},
]


def _hm(s: str):
    h, m = str(s).split(":")
    return int(h), int(m)


def _windows_around(local_now, sh, sm, eh, em):
    """Candidate (start, end) windows for yesterday/today/tomorrow, supporting
    windows that cross local midnight (end <= start)."""
    out = []
    crosses = (eh, em) <= (sh, sm)
    for off in (-1, 0, 1):
        base = (local_now + dt.timedelta(days=off)).replace(
            hour=sh, minute=sm, second=0, microsecond=0)
        end = base.replace(hour=eh, minute=em)
        if crosses:
            end = end + dt.timedelta(days=1)
        out.append((base, end))
    return out


def session_status(sess: dict, now_utc: dt.datetime) -> dict:
    """Active / next-boundary status for one session at `now_utc` (tz-aware)."""
    try:
        tz = ZoneInfo(sess["tz"])
    except Exception:
        tz = dt.timezone.utc
    local = now_utc.astimezone(tz)
    sh, sm = _hm(sess.get("start", "00:00"))
    eh, em = _hm(sess.get("end", "00:00"))
    wins = _windows_around(local, sh, sm, eh, em)

    active, closes_at = False, None
    for s, e in wins:
        if s <= local < e:
            active, closes_at = True, e
            break
    opens_at = None
    if not active:
        future = [s for s, e in wins if s > local]
        opens_at = min(future) if future else None

    def _mins(x):
        return None if x is None else max(0, int((x - local).total_seconds() // 60))

    def _utc(x):
        return None if x is None else x.astimezone(dt.timezone.utc).isoformat()

    return {
        "id": sess["id"], "label": sess.get("label", sess["id"]), "tz": sess["tz"],
        "enabled": bool(sess.get("enabled", True)),
        "start": sess.get("start"), "end": sess.get("end"),
        "active": active,
        "opens_in_min": _mins(opens_at), "closes_in_min": _mins(closes_at),
        "opens_at_utc": _utc(opens_at), "closes_at_utc": _utc(closes_at),
    }


def status(sessions: List[dict], now_utc: dt.datetime) -> dict:
    per = [session_status(s, now_utc) for s in (sessions or DEFAULT_SESSIONS)]
    return {"active": [s["label"] for s in per if s["active"] and s["enabled"]],
            "windows": per}
