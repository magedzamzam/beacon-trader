"""Pure helpers of the notification ctx builder (#139 test-fire). The full
build_ctx needs a DB session and is exercised end-to-end via the API."""
import datetime as dt
from types import SimpleNamespace as NS

from beacon_core.notifications import context as C


def test_fmt_time():
    assert C._fmt_time(dt.datetime(2026, 7, 26, 14, 32)) == "2026-07-26 14:32"
    assert C._fmt_time(None) is None
    assert C._fmt_time("not a date") is None          # bad input -> None, never raises


def test_entry_single_and_band():
    assert C._entry(NS(entry_from=3421, entry_to=3421)) == "3421"     # equal -> single
    assert C._entry(NS(entry_from=3421, entry_to=None)) == "3421"
    assert C._entry(NS(entry_from=4025, entry_to=4020)) == "4025–4020"  # band
    assert C._entry(None) is None
    assert C._entry(NS(entry_from=None, entry_to=None)) is None
