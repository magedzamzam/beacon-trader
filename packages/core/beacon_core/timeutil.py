"""Shared time helpers (#36): tz-aware UTC now + robust ISO-UTC parsing that was
reimplemented (with subtly different naive/aware handling) in several modules."""
from __future__ import annotations

import datetime as dt
from typing import Optional


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_iso_utc(v) -> Optional[dt.datetime]:
    """Parse an ISO date/datetime to a tz-aware UTC datetime (a naive value is
    treated as UTC). Returns None on empty/unparseable input."""
    if not v:
        return None
    try:
        d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
