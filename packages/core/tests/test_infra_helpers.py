"""Shared infra helpers extracted in #36: timeutil (utcnow / parse_iso_utc) and
tasks (spawn_bg). Pure — no DB/crypto deps, runs on a bare box."""
import asyncio
import datetime as dt

from beacon_core.timeutil import utcnow, parse_iso_utc
from beacon_core.tasks import spawn_bg, _BG


def test_utcnow_is_tz_aware_utc():
    now = utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == dt.timedelta(0)


def test_parse_iso_utc_variants():
    # trailing Z
    d = parse_iso_utc("2026-07-10T12:00:00Z")
    assert d == dt.datetime(2026, 7, 10, 12, 0, tzinfo=dt.timezone.utc)
    # explicit offset is normalized to the same instant (tz preserved, UTC instant equal)
    d2 = parse_iso_utc("2026-07-10T14:00:00+02:00")
    assert d2 == dt.datetime(2026, 7, 10, 12, 0, tzinfo=dt.timezone.utc)
    # naive is treated as UTC
    d3 = parse_iso_utc("2026-07-10T12:00:00")
    assert d3.tzinfo is not None and d3.utcoffset() == dt.timedelta(0)
    # date-only
    assert parse_iso_utc("2026-07-10").tzinfo is not None


def test_parse_iso_utc_bad_input_returns_none():
    for bad in (None, "", "not-a-date", 12345, [], {}):
        assert parse_iso_utc(bad) is None


def test_spawn_bg_keeps_ref_and_runs():
    ran = {"v": False}

    async def _work():
        await asyncio.sleep(0)
        ran["v"] = True

    async def _main():
        t = spawn_bg(_work())
        assert t in _BG                     # strong ref held while pending
        await t
        return t

    t = asyncio.run(_main())
    assert ran["v"] is True
    assert t not in _BG                     # discarded on completion


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("ALL PASS")
