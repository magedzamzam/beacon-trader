"""The per-day gap audit.

Aggregate density is not coverage. A window can be 70% populated — exactly what
a five-day-a-week instrument should be — while one Tuesday is missing entirely,
and every signal on that Tuesday would replay against a price series that simply
stops. #169 §3 requires gaps to be KNOWN rather than silently treated as flat
price, so this is the check that makes the difference visible.
"""
from __future__ import annotations

import datetime as dt

from harness import bars as B
from conftest import path_bars

# 2026-07-06 is a Monday.
MON = dt.datetime(2026, 7, 6, 0, 0, tzinfo=dt.timezone.utc)


def _week(per_day: dict, start=MON, minutes_apart=1):
    """One bar per minute per listed day-offset, so counts are exact."""
    out = []
    for offset, n in sorted(per_day.items()):
        day = start + dt.timedelta(days=offset)
        out.extend(path_bars([4000] * n, start=day, step_minutes=minutes_apart))
    return out


def test_a_full_working_week_reports_no_gaps():
    out = B.daily_gaps(_week({0: 100, 1: 100, 2: 100, 3: 100, 4: 100}))
    assert out["missing_weekdays"] == []
    assert out["thin_weekdays"] == []
    assert out["median_weekday_bars"] == 100
    assert out["n_days"] == 5


def test_weekends_are_absent_by_design_and_never_reported():
    """Saturday and Sunday have no bars for a reason that is not a data problem;
    listing them would bury a real hole in noise."""
    out = B.daily_gaps(_week({0: 100, 1: 100, 2: 100, 3: 100, 4: 100,
                              7: 100}))   # next Monday, skipping Sat+Sun
    assert out["missing_weekdays"] == []


def test_a_missing_weekday_is_reported():
    out = B.daily_gaps(_week({0: 100, 1: 100, 3: 100, 4: 100}))   # no Wednesday
    assert out["missing_weekdays"] == ["2026-07-08"]


def test_a_thin_weekday_is_reported_with_its_count():
    out = B.daily_gaps(_week({0: 100, 1: 100, 2: 10, 3: 100, 4: 100}))
    assert out["thin_weekdays"] == [{"date": "2026-07-08", "n_bars": 10}]
    assert out["missing_weekdays"] == []


def test_a_merely_quiet_day_is_not_flagged():
    """The threshold is half the median — a session that traded 70% of normal is
    a quiet session, not a hole, and flagging it would train the reader to
    ignore the field."""
    out = B.daily_gaps(_week({0: 100, 1: 100, 2: 70, 3: 100, 4: 100}))
    assert out["thin_weekdays"] == []


def test_the_audit_respects_the_window_start():
    """`coverage --since` bounds the question; a gap before the live window is
    not a gap in the data the run will use."""
    bars = _week({0: 100, 1: 100, 3: 100, 4: 100})            # Wednesday missing
    frm = MON + dt.timedelta(days=3)                           # start on Thursday
    out = B.daily_gaps(bars, frm=frm)
    assert out["missing_weekdays"] == []
    assert out["n_days"] == 2


def test_an_empty_series_does_not_raise():
    out = B.daily_gaps([])
    assert out["n_days"] == 0 and out["median_weekday_bars"] is None


def test_gaps_are_bounded_by_the_data_not_by_today():
    """The scan runs first-bar to last-bar. Extending it to `now` would report
    every day since the feed stopped as missing, which is one fact reported N
    times and drowns the real holes."""
    out = B.daily_gaps(_week({0: 100, 1: 100}))
    assert out["missing_weekdays"] == []
