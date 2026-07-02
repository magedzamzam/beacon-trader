"""Unit tests for the executor's live-execution guard (PM bot, 2026-07-02).

These test the guard *logic* in isolation so they run without the full app/DB
stack. They mirror the checks in services/executor/main.handle_signal:
  1. source must be enabled_for_trading
  2. source must be is_trusted
  3. source name must not contain a blocklisted token
"""
from dataclasses import dataclass

EXECUTION_NAME_BLOCKLIST = ("test", "sample", "demo")


@dataclass
class FakeSource:
    id: int
    name: str
    enabled_for_trading: bool
    is_trusted: bool


def should_execute(source: FakeSource) -> bool:
    if not source or not source.enabled_for_trading:
        return False
    if not source.is_trusted:
        return False
    if any(tok in (source.name or "").lower() for tok in EXECUTION_NAME_BLOCKLIST):
        return False
    return True


def test_untrusted_source_blocked():
    assert should_execute(FakeSource(3, "Euvean Gold Trader", True, False)) is False


def test_test_named_source_blocked():
    assert should_execute(FakeSource(8, "Test", True, True)) is False


def test_sample_and_demo_blocked():
    assert should_execute(FakeSource(9, "Sample Gold Channel", True, True)) is False
    assert should_execute(FakeSource(10, "Demo Desk", True, True)) is False


def test_disabled_source_blocked():
    assert should_execute(FakeSource(4, "GOLD VIP SIGNAL TM", False, True)) is False


def test_trusted_enabled_source_allowed():
    assert should_execute(FakeSource(4, "GOLD VIP SIGNAL TM", True, True)) is True
