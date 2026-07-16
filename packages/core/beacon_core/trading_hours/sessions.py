"""Trading-session windows, computed live in each market's local timezone so
DST is handled automatically. Windows are configurable; these are the defaults.
"""
from __future__ import annotations

import datetime as dt
from typing import List
from zoneinfo import ZoneInfo

# Default session windows in each market's LOCAL time (DST-aware via tz).
# risk_mult (#81) scales the risk budget for entries opened while the session is
# active (de-size only, 0..1). New York defaults to 0.5 so the London/NY overlap
# — which held 73% of losses (hour-12 = 43%, win 33-41%) — is halved, while
# London and Asian (61-65% win) stay full. The overlap combines multiplicatively.
DEFAULT_SESSIONS = [
    {"id": "asian", "label": "Asian (Tokyo)", "tz": "Asia/Tokyo",
     "start": "09:00", "end": "18:00", "enabled": True, "risk_mult": 1.0},
    {"id": "london", "label": "London", "tz": "Europe/London",
     "start": "08:00", "end": "17:00", "enabled": True, "risk_mult": 1.0},
    {"id": "newyork", "label": "New York", "tz": "America/New_York",
     "start": "08:00", "end": "17:00", "enabled": True, "risk_mult": 0.5},
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

    # This session's window on the CURRENT UTC day, as fractional hours from UTC
    # midnight (may be < 0 or > 24 for windows that straddle the day) — used to
    # draw the timeline bar. DST-correct because we go through the session tz.
    utc = dt.timezone.utc
    open_local = local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    close_local = local.replace(hour=eh, minute=em, second=0, microsecond=0)
    if (eh, em) <= (sh, sm):
        close_local = close_local + dt.timedelta(days=1)
    midnight = now_utc.astimezone(utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start_h = (open_local.astimezone(utc) - midnight).total_seconds() / 3600.0
    end_h = (close_local.astimezone(utc) - midnight).total_seconds() / 3600.0

    try:
        risk_mult = max(0.0, min(1.0, float(sess.get("risk_mult", 1.0))))
    except (TypeError, ValueError):
        risk_mult = 1.0

    return {
        "id": sess["id"], "label": sess.get("label", sess["id"]), "tz": sess["tz"],
        "enabled": bool(sess.get("enabled", True)), "risk_mult": risk_mult,
        "start": sess.get("start"), "end": sess.get("end"),
        "active": active,
        "opens_in_min": _mins(opens_at), "closes_in_min": _mins(closes_at),
        "opens_at_utc": _utc(opens_at), "closes_at_utc": _utc(closes_at),
        "start_hour_utc": round(start_h, 3), "end_hour_utc": round(end_h, 3),
    }


def risk_multiplier(sessions: List[dict], now_utc: dt.datetime) -> float:
    """Combined session risk multiplier at `now` (#81): the product of risk_mult
    over every ACTIVE, enabled session (1.0 when none are active or all are full).
    So the London/NY overlap multiplies both windows' factors — de-size only."""
    mult = 1.0
    for s in (sessions or DEFAULT_SESSIONS):
        st = session_status(s, now_utc)
        if st["active"] and st["enabled"]:
            mult *= st["risk_mult"]
    return max(0.0, min(1.0, mult))


def status(sessions: List[dict], now_utc: dt.datetime) -> dict:
    per = [session_status(s, now_utc) for s in (sessions or DEFAULT_SESSIONS)]
    now = now_utc.astimezone(dt.timezone.utc)
    return {"active": [s["label"] for s in per if s["active"] and s["enabled"]],
            "now_hour_utc": round(now.hour + now.minute / 60.0, 3),
            "risk_multiplier": risk_multiplier(sessions, now_utc),
            "windows": per}
