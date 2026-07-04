"""US market (NYSE) holiday calendar + weekend status — computed, no external
data. Holidays follow fixed rules (nth weekday, observed shifts, Good Friday via
Easter), so any year is derivable."""
from __future__ import annotations

import datetime as dt
from datetime import date, timedelta


def easter(year: int) -> date:
    """Gregorian Easter Sunday (Anonymous Computus)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:            # Saturday -> Friday
        return d - timedelta(days=1)
    if d.weekday() == 6:            # Sunday -> Monday
        return d + timedelta(days=1)
    return d


def us_market_holidays(year: int) -> dict:
    """{date: name} for the given year (NYSE full-day closures)."""
    h = {}
    h[_observed(date(year, 1, 1))] = "New Year's Day"
    h[_nth_weekday(year, 1, 0, 3)] = "MLK Jr. Day"
    h[_nth_weekday(year, 2, 0, 3)] = "Presidents' Day"
    h[easter(year) - timedelta(days=2)] = "Good Friday"
    h[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        h[_observed(date(year, 6, 19))] = "Juneteenth"
    h[_observed(date(year, 7, 4))] = "Independence Day"
    h[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    h[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving"     # 4th Thursday
    h[_observed(date(year, 12, 25))] = "Christmas"
    return h


def is_us_holiday(d: date):
    return us_market_holidays(d.year).get(d)


def status(now_utc: dt.datetime) -> dict:
    d = now_utc.date()
    this_year = us_market_holidays(d.year)
    name = this_year.get(d)
    is_weekend = now_utc.weekday() >= 5      # Sat=5, Sun=6

    combined = dict(this_year)
    combined.update(us_market_holidays(d.year + 1))
    upcoming = sorted(x for x in combined if x >= d)
    nxt = upcoming[0] if upcoming else None
    return {
        "is_weekend": is_weekend,
        "is_holiday": name is not None,
        "holiday_name": name,
        "next_holiday": None if nxt is None else {
            "name": combined[nxt], "date": nxt.isoformat(), "in_days": (nxt - d).days},
    }
