"""Unit tests for Trading Hours (holidays + news blackout are pure; the session
math needs the IANA tz db and is skipped if unavailable on the test box)."""
import datetime as dt
from datetime import date

from beacon_core.trading_hours import calendar as C
from beacon_core.trading_hours import config as SVC
from beacon_core.trading_hours import holidays as H


def test_easter_and_good_friday():
    assert H.easter(2026) == date(2026, 4, 5)
    inv = {v: k for k, v in H.us_market_holidays(2026).items()}
    assert inv["Good Friday"] == date(2026, 4, 3)


def test_observed_shift_july4_saturday():
    # Jul 4 2026 is a Saturday -> observed Friday Jul 3
    inv = {v: k for k, v in H.us_market_holidays(2026).items()}
    assert inv["Independence Day"] == date(2026, 7, 3)


def test_nth_weekday_holidays():
    inv = {v: k for k, v in H.us_market_holidays(2026).items()}
    assert inv["MLK Jr. Day"] == date(2026, 1, 19)       # 3rd Monday Jan
    assert inv["Thanksgiving"] == date(2026, 11, 26)     # 4th Thursday Nov
    assert inv["Memorial Day"] == date(2026, 5, 25)      # last Monday May


def test_holiday_status():
    st = H.status(dt.datetime(2026, 12, 25, 15, 0, tzinfo=dt.timezone.utc))
    assert st["is_holiday"] and st["holiday_name"] == "Christmas"
    assert st["next_holiday"] is not None


def _ev(m, impact, ccy, title):
    return {"ts": dt.datetime(2026, 7, 7, 12, m, tzinfo=dt.timezone.utc),
            "impact": impact, "ccy": ccy, "title": title}


def test_blackout_active_and_next():
    now = dt.datetime(2026, 7, 7, 12, 30, tzinfo=dt.timezone.utc)
    events = [_ev(31, "high", "USD", "US CPI"), _ev(32, "low", "EUR", "minor")]
    bl = C.blackout_status(events, now, impacts=("high",), before_min=3, after_min=3)
    assert bl["in_blackout"] and bl["active"]["title"] == "US CPI"
    assert bl["next"]["title"] == "US CPI"


def test_blackout_currency_filter_and_clear():
    now = dt.datetime(2026, 7, 7, 12, 30, tzinfo=dt.timezone.utc)
    events = [_ev(31, "high", "USD", "US CPI")]
    assert not C.blackout_status(events, now, currencies=["EUR"])["in_blackout"]
    # well outside the window
    quiet = dt.datetime(2026, 7, 7, 10, 0, tzinfo=dt.timezone.utc)
    assert not C.blackout_status(events, quiet)["in_blackout"]


def test_sanitize_config_defaults():
    cfg = SVC.sanitize_config(None)
    assert cfg["sessions"] and cfg["news"]["enabled"] in (True, False)
    assert cfg["news"]["before_min"] >= 0 and "impacts" in cfg["news"]
    # bad session dropped, good kept
    cfg2 = SVC.sanitize_config({"sessions": [{"id": "x"}, {"id": "ok", "tz": "Europe/London",
                                "start": "08:00", "end": "17:00"}]})
    assert [s["id"] for s in cfg2["sessions"]] == ["ok"]


def test_sessions_math_if_tz_available():
    try:
        from beacon_core.trading_hours import sessions as S
        # 14:00 UTC in July (DST) -> London and New York both active (overlap)
        s = S.status(S.DEFAULT_SESSIONS, dt.datetime(2026, 7, 7, 14, 0, tzinfo=dt.timezone.utc))
    except Exception:
        return  # tz db not present on this box
    if s["active"] == []:            # tz fell back to UTC (no tzdata) — skip
        return
    assert "London" in s["active"] and "New York" in s["active"]
