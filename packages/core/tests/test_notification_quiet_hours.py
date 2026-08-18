"""Quiet hours + the min-severity delivery gate (#210).

Every routed event fired 24/7 at the same weight, so on a busy XAUUSD day the
routine stream (new_signal, order_placed, order_filled, order_cancelled) buried
the money and safety events, and there was no way to say "overnight, only the
critical ones". `throttle.py` (#180) collapses ONE event's bursts; this is the
cross-event, time-of-day axis it cannot see.

Two properties carry the whole feature and both are asymmetric on purpose:
a HOLD is recorded, never silent; and every path that cannot be read - disabled
policy, malformed window, unknown severity, unmapped event - resolves to
DELIVER. A notification gate that fails closed is a gate that eats a stop-out.
"""
import asyncio
import datetime as dt

import pytest

from beacon_core.notifications import config as C
from beacon_core.notifications import dispatch as D

QUIET = {"enabled": True, "start_utc": "22:00", "end_utc": "06:00",
         "min_severity": "critical"}


def _at(h, m=0):
    return dt.datetime(2026, 8, 18, h, m, tzinfo=dt.timezone.utc)


# --- the severity map ---------------------------------------------------------
def test_money_and_safety_events_are_critical():
    for e in ("sl_hit", "tp_hit", "trade_closed", "broker_error", "arm_dark"):
        assert C.event_severity(e) == "critical", e


def test_routine_flow_is_info_and_the_digest_sits_between():
    for e in ("new_signal", "order_placed", "order_filled", "order_cancelled"):
        assert C.event_severity(e) == "info", e
    assert C.event_severity("daily_summary") == "summary"
    assert C.SEVERITY_RANK["info"] < C.SEVERITY_RANK["summary"] < C.SEVERITY_RANK["critical"]


def test_an_unmapped_event_is_critical_not_silenceable():
    """A new event type must not become mutable by having been forgotten here."""
    assert C.event_severity("some_event_shipped_next_week") == "critical"


def test_every_routed_event_has_a_severity():
    assert set(C.EVENT_SEVERITY) == set(C.EVENT_IDS)


# --- the window ---------------------------------------------------------------
@pytest.mark.parametrize("hour,inside", [
    (21, False), (22, True), (23, True), (0, True), (5, True), (6, False), (12, False)])
def test_the_overnight_window_crosses_midnight(hour, inside):
    assert C.in_quiet_window(QUIET, _at(hour)) is inside


def test_a_same_day_window_is_half_open():
    day = {**QUIET, "start_utc": "09:00", "end_utc": "17:00"}
    assert C.in_quiet_window(day, _at(9)) is True
    assert C.in_quiet_window(day, _at(16, 59)) is True
    assert C.in_quiet_window(day, _at(17)) is False


@pytest.mark.parametrize("policy", [
    {"start_utc": "banana", "end_utc": "06:00"},
    {"start_utc": "22:00", "end_utc": None},
    {"start_utc": "22:00", "end_utc": "22:00"},          # empty window
    {},
])
def test_a_window_that_cannot_be_read_is_never_quiet(policy):
    assert C.in_quiet_window(policy, _at(23)) is False


# --- the gate -----------------------------------------------------------------
def test_inside_the_window_routine_events_are_held_and_money_events_are_not():
    assert C.is_quiet_suppressed("order_placed", QUIET, _at(23)) is True
    assert C.is_quiet_suppressed("daily_summary", QUIET, _at(23)) is True
    assert C.is_quiet_suppressed("sl_hit", QUIET, _at(23)) is False
    assert C.is_quiet_suppressed("broker_error", QUIET, _at(23)) is False


def test_outside_the_window_everything_routes_as_today():
    for e in C.EVENT_IDS:
        assert C.is_quiet_suppressed(e, QUIET, _at(12)) is False


def test_a_summary_floor_holds_chatter_but_passes_the_digest():
    policy = {**QUIET, "min_severity": "summary"}
    assert C.is_quiet_suppressed("order_placed", policy, _at(23)) is True
    assert C.is_quiet_suppressed("daily_summary", policy, _at(23)) is False


@pytest.mark.parametrize("policy", [
    {**QUIET, "enabled": False},
    {**QUIET, "min_severity": "urgent-ish"},
    {**QUIET, "min_severity": None},
    {},
    None,
])
def test_every_unreadable_policy_delivers(policy):
    """The only way to stop a notification is a policy that explicitly says so."""
    assert C.is_quiet_suppressed("order_placed", policy, _at(23)) is False


# --- dispatch -----------------------------------------------------------------
class _Session:
    """Enough session for notify(): a settings map plus a delivery sink."""

    def __init__(self, settings):
        self.settings = settings
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def rollback(self):                    # pragma: no cover - failure path
        pass

    async def execute(self, *a, **k):            # pragma: no cover - guard
        raise AssertionError("unexpected query")


def _wire(monkeypatch, quiet, when):
    sent = []

    async def _get_setting(session, key, default=None):
        return session.settings.get(key, default)

    async def _sender(cfg, subject, text):
        sent.append(subject)

    monkeypatch.setattr(D, "get_setting", _get_setting)
    monkeypatch.setattr(D, "utcnow", lambda: when)
    monkeypatch.setattr(D, "SENDERS", {"telegram": _sender})
    stored = {"channels": {"telegram": {"enabled": True, "bot_token": "t",
                                        "chat_id": "1"}},
              "routing": {e: ["telegram"] for e in C.EVENT_IDS}}
    return sent, _Session({C.SETTING_KEY: stored, D.QUIET_HOURS_KEY: quiet})


def test_a_held_event_is_recorded_rather_than_silently_dropped(monkeypatch):
    """Never silent: #181's tripwire has to see the hold, or "why didn't I get an
    alert?" has no answer and the operator stops trusting the channel."""
    sent, s = _wire(monkeypatch, QUIET, _at(23))
    out = asyncio.run(D.notify(s, "order_placed", {"symbol": "XAUUSD"}))
    assert sent == []                                   # nothing delivered
    assert out["suppressed"] == "quiet_hours"
    assert out["results"] == {"telegram": D.SUPPRESSED_QUIET}
    (row,) = [o for o in s.added if getattr(o, "event_id", None) == "order_placed"]
    assert row.results == {"telegram": D.SUPPRESSED_QUIET}
    assert row.ok is False
    assert row.subject                                  # the message it stood for


def test_an_sl_hit_still_delivers_inside_the_window(monkeypatch):
    sent, s = _wire(monkeypatch, QUIET, _at(23))
    out = asyncio.run(D.notify(s, "sl_hit", {"symbol": "XAUUSD", "pl": -40}))
    assert out["results"] == {"telegram": "ok"}
    assert "suppressed" not in out
    assert len(sent) == 1 and "P&L -40.00" in sent[0]


def test_outside_the_window_the_routine_event_delivers(monkeypatch):
    sent, s = _wire(monkeypatch, QUIET, _at(12))
    out = asyncio.run(D.notify(s, "order_placed", {"symbol": "XAUUSD"}))
    assert out["results"] == {"telegram": "ok"} and len(sent) == 1


def test_disabled_by_default_behaviour_is_byte_identical(monkeypatch):
    """Nothing changes until an operator opts in."""
    sent, s = _wire(monkeypatch, {}, _at(23))
    for e in ("order_placed", "daily_summary", "sl_hit"):
        assert asyncio.run(D.notify(s, e, {}))["results"] == {"telegram": "ok"}
    assert len(sent) == 3


def test_an_unroutable_event_is_not_reported_as_quiet_suppressed(monkeypatch):
    """No routing is a different answer from held, and #181 distinguishes them."""
    sent, s = _wire(monkeypatch, QUIET, _at(23))
    s.settings[C.SETTING_KEY]["routing"]["order_placed"] = []
    out = asyncio.run(D.notify(s, "order_placed", {}))
    assert out["results"] == {} and "suppressed" not in out


# --- policy validation --------------------------------------------------------
def test_quiet_hours_policy_is_whitelisted_and_clamped():
    out = C.clean_policy("quiet_hours", {"enabled": "yes", "start_utc": "22:00",
                                         "end_utc": "banana",
                                         "min_severity": "URGENT",
                                         "rm -rf": "/"})
    assert out == {"enabled": True, "start_utc": "22:00", "end_utc": "06:00",
                   "min_severity": "critical"}


def test_daily_summary_policy_rejects_a_time_the_monitor_could_not_read():
    assert C.clean_policy("daily_summary", {"enabled": True,
                                            "at_utc": "banana"})["at_utc"] == "00:15"
    assert C.clean_policy("daily_summary", {"at_utc": "23:45"})["at_utc"] == "23:45"


def test_arm_dark_policy_clamps_its_ranges():
    out = C.clean_policy("arm_dark", {"enabled": True, "threshold": 5,
                                      "window_hours": 0, "min_signals": "12",
                                      "cooldown_hours": 9999})
    assert out["threshold"] == 1.0 and out["window_hours"] == 1
    assert out["min_signals"] == 12 and out["cooldown_hours"] == 168


def test_an_unknown_policy_key_is_refused():
    with pytest.raises(ValueError):
        C.clean_policy("delete_everything", {})


def test_the_catalog_publishes_the_mapping_the_operator_is_setting():
    cat = C.catalog()
    assert cat["severity"]["sl_hit"] == "critical"
    assert cat["severity"]["order_placed"] == "info"
    assert cat["severities"] == C.SEVERITIES
