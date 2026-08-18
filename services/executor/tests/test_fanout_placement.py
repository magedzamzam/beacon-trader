"""The executor places the fanout in the seeded order and records the lag (#211).

Static assertions over the module source rather than a live run: `handle_signal`
needs a broker, a DB and a queue, and none of them are what is under test here —
what matters is that the trading path calls the seeded permutation instead of
iterating the account rows, and that the handicap it cannot avoid is at least
persisted per trade.
"""
import re
from pathlib import Path

MAIN = Path(__file__).resolve().parents[1] / "main.py"
SRC = MAIN.read_text(encoding="utf-8")


def test_the_fanout_iterates_the_seeded_order_not_the_row_order():
    assert "fanout_order(" in SRC
    # the old, confounded loop must be gone
    assert not re.search(r"^\s+for acct in accounts:", SRC, re.M)


def test_every_placement_records_how_late_in_the_queue_it_was():
    assert "placement_lag_ms=_lag_ms" in SRC
    assert "placement_lag_ms=placement_lag_ms" in SRC


def test_the_lag_is_measured_from_the_first_placement_of_the_same_signal():
    """Relative to the FIRST arm, which is what makes it comparable across
    signals — an absolute timestamp would just re-encode when the signal
    arrived."""
    assert "_fanout_started = time.monotonic()" in SRC
    assert "time.monotonic() - _fanout_started" in SRC


def test_the_column_has_a_startup_alter():
    """`create_all` does not add columns to an existing table (CLAUDE.md 6), so
    a new column with no ALTER is a column that never appears on the live box."""
    from beacon_core.db.base import ADDITIVE_MIGRATIONS
    assert any("placement_lag_ms" in stmt for stmt in ADDITIVE_MIGRATIONS)
