"""Trading-session tagging by GMT hour. Single source of truth so the
collector, regime service and backtester all agree on session boundaries.

Boundaries (GMT): ASIA 00-07, LONDON 07-12, OVERLAP 12-16, NY 16-21, LATE 21-24.
"""
from __future__ import annotations

import datetime as dt

SESSIONS = ("ASIA", "LONDON", "OVERLAP", "NY", "LATE")


def session_for(ts: dt.datetime) -> str:
    """Return the session tag for a timestamp. Tz-aware inputs are converted to
    UTC; naive inputs are assumed to already be UTC/GMT."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(dt.timezone.utc)
    h = ts.hour
    if h < 7:
        return "ASIA"
    if h < 12:
        return "LONDON"
    if h < 16:
        return "OVERLAP"
    if h < 21:
        return "NY"
    return "LATE"
