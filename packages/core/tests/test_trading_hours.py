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


def test_tiered_blackout_major_widens_window(monkeypatch=None):
    # #77: at T-9m a standard ±3 window is clear, but a MAJOR (CPI) -30/+15
    # window is active — the 07-14 CPI cluster fired 4-9 min before the print.
    kws = SVC.DEFAULT_MAJOR_KEYWORDS
    events = [_ev(30, "high", "USD", "US CPI m/m")]      # print at 12:30
    t_minus_9 = dt.datetime(2026, 7, 7, 12, 21, tzinfo=dt.timezone.utc)
    std = C.blackout_status(events, t_minus_9, before_min=3, after_min=3)
    assert not std["in_blackout"]                       # old behaviour: sailed through
    maj = C.blackout_status(events, t_minus_9, before_min=3, after_min=3,
                            major_before_min=30, major_after_min=15, major_keywords=kws)
    assert maj["in_blackout"] and maj["active"]["tier"] == "major"
    assert maj["active"]["in_min"] == 9


def test_tiered_blackout_non_major_keeps_tight_window():
    # A high-impact event NOT in the keyword list keeps the ±3 window.
    events = [_ev(30, "high", "USD", "Retail Sales m/m")]
    t_minus_9 = dt.datetime(2026, 7, 7, 12, 21, tzinfo=dt.timezone.utc)
    bl = C.blackout_status(events, t_minus_9, before_min=3, after_min=3,
                           major_before_min=30, major_after_min=15,
                           major_keywords=SVC.DEFAULT_MAJOR_KEYWORDS)
    assert not bl["in_blackout"]                        # not a major -> tight window


def test_blackout_backward_compatible_without_major_args():
    # Omitting major_* reproduces the original single-tier behaviour exactly.
    events = [_ev(30, "high", "USD", "US CPI")]
    now = dt.datetime(2026, 7, 7, 12, 21, tzinfo=dt.timezone.utc)
    assert not C.blackout_status(events, now, before_min=3, after_min=3)["in_blackout"]


def test_sanitize_news_tier_backward_compat():
    # a stored pre-#77 news row (no tier fields) still parses and gets defaults;
    # major window is never narrower than the standard window.
    cfg = SVC.sanitize_config({"news": {"enabled": True, "before_min": 5, "after_min": 5}})
    n = cfg["news"]
    assert n["gate_entries"] is True
    assert n["major_before_min"] >= n["before_min"] and n["major_after_min"] >= n["after_min"]
    assert n["major_keywords"]                          # populated from defaults
    # an operator narrowing major below standard is lifted to the standard
    cfg2 = SVC.sanitize_config({"news": {"before_min": 20, "major_before_min": 5}})
    assert cfg2["news"]["major_before_min"] == 20


def test_sanitize_config_defaults():
    cfg = SVC.sanitize_config(None)
    assert cfg["sessions"] and cfg["news"]["enabled"] in (True, False)
    assert cfg["news"]["before_min"] >= 0 and "impacts" in cfg["news"]
    # bad session dropped, good kept
    cfg2 = SVC.sanitize_config({"sessions": [{"id": "x"}, {"id": "ok", "tz": "Europe/London",
                                "start": "08:00", "end": "17:00"}]})
    assert [s["id"] for s in cfg2["sessions"]] == ["ok"]


def test_session_risk_mult_sanitized_and_clamped():
    # #81: risk_mult defaults to 1.0, is clamped to [0,1], and survives round-trip.
    cfg = SVC.sanitize_config({"sessions": [
        {"id": "a", "tz": "UTC", "start": "00:00", "end": "23:59", "risk_mult": 0.5},
        {"id": "b", "tz": "UTC", "start": "00:00", "end": "23:59", "risk_mult": 9},   # clamp -> 1.0
        {"id": "c", "tz": "UTC", "start": "00:00", "end": "23:59"}]})                  # default -> 1.0
    mults = {s["id"]: s["risk_mult"] for s in cfg["sessions"]}
    assert mults == {"a": 0.5, "b": 1.0, "c": 1.0}


def test_session_risk_multiplier_overlap_if_tz_available():
    try:
        from beacon_core.trading_hours import sessions as S
        # 14:00 UTC July (DST): London + New York both active (overlap). Defaults
        # give NY risk_mult 0.5 -> combined 1.0 x 0.5 = 0.5.
        now = dt.datetime(2026, 7, 7, 14, 0, tzinfo=dt.timezone.utc)
        st = S.status(S.DEFAULT_SESSIONS, now)
    except Exception:
        return
    if st["active"] == []:            # tz fell back to UTC (no tzdata) — skip
        return
    if "London" in st["active"] and "New York" in st["active"]:
        assert abs(st["risk_multiplier"] - 0.5) < 1e-9
    # a deep-Asian-only hour should be full size
    asia = S.risk_multiplier(S.DEFAULT_SESSIONS, dt.datetime(2026, 7, 7, 1, 0, tzinfo=dt.timezone.utc))
    assert asia == 1.0


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
