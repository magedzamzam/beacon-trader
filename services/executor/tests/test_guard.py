"""Unit tests for the executor's live-execution guard.

These now exercise the REAL guard in beacon_core.execution.guard (pure, no
DB/app stack) which the executor calls in handle_signal:
  1. source must be enabled_for_trading
  2. source name must not contain a blocklisted token (test/sample/demo)
  3. source must be is_trusted, unless allow_untrusted is set
"""
from beacon_core.execution.guard import should_auto_execute


def _ok(**kw):
    return should_auto_execute(**kw)[0]


def test_untrusted_source_blocked():
    assert _ok(enabled_for_trading=True, is_trusted=False, name="Euvean Gold Trader") is False


def test_untrusted_allowed_with_override():
    assert _ok(enabled_for_trading=True, is_trusted=False, name="Euvean Gold Trader",
               allow_untrusted=True) is True


def test_test_named_source_blocked():
    assert _ok(enabled_for_trading=True, is_trusted=True, name="Test") is False


def test_sample_and_demo_blocked():
    assert _ok(enabled_for_trading=True, is_trusted=True, name="Sample Gold Channel") is False
    assert _ok(enabled_for_trading=True, is_trusted=True, name="Demo Desk") is False


def test_disabled_source_blocked():
    assert _ok(enabled_for_trading=False, is_trusted=True, name="GOLD VIP SIGNAL TM") is False


def test_trusted_enabled_source_allowed():
    assert _ok(enabled_for_trading=True, is_trusted=True, name="GOLD VIP SIGNAL TM") is True


def test_reason_is_reported():
    ok, reason = should_auto_execute(enabled_for_trading=True, is_trusted=False, name="X")
    assert ok is False and "trust" in reason.lower()
