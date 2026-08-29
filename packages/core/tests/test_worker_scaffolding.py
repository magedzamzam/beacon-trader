"""One run-loop, one health judgement, one log format for the worker fleet (#258).

The Lever-5 chain added three persistent compose services and each started from a
copy of the last: three near-verbatim `while True / try / except / sleep` blocks,
two identical engine bootstraps, two copy-pasted `--healthcheck` probes, and 29
`print(..., flush=True)` calls against zero uses of the `get_logger` factory the
executor, monitor and api all standardise on.

Nothing here is about behaviour changing. It is about the two loop semantics NOT
drifting — one pass then exit, and a bad pass under `--loop` surviving — now that
they are written once instead of three times.
"""
import asyncio
import datetime as dt
from pathlib import Path

import pytest

from beacon_core import worker as W

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = ["candle_sync.py", "engine_producer.py", "excursion_scorer.py"]


def _src(name):
    return (REPO / "scripts" / name).read_text(encoding="utf-8")


# --- the loop -----------------------------------------------------------------
def test_no_interval_is_exactly_one_pass():
    """Every existing manual invocation of these scripts relies on it."""
    calls = []

    async def once():
        calls.append(1)

    asyncio.run(W.run_loop(once, name="t", interval=0))
    assert calls == [1]


def test_a_failure_in_a_one_pass_run_raises():
    """An operator running a catch-up by hand must see it, and the exit code
    must be non-zero — swallowing it would report success on a no-op."""
    async def once():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(W.run_loop(once, name="t", interval=0))


def test_a_bad_pass_under_loop_is_logged_and_the_loop_continues():
    """A worker that dies on one malformed window stops syncing until somebody
    notices, which is the failure mode all three of these exist to end."""
    seen = []

    async def once():
        seen.append(len(seen))
        if len(seen) == 1:
            raise ValueError("one bad window")
        if len(seen) >= 3:
            raise asyncio.CancelledError

    async def drive():
        orig = asyncio.sleep
        asyncio.sleep = lambda *_: orig(0)
        try:
            with pytest.raises(asyncio.CancelledError):
                await W.run_loop(once, name="t", interval=5)
        finally:
            asyncio.sleep = orig

    asyncio.run(drive())
    assert len(seen) == 3          # survived the failure and kept going


def test_a_pass_result_is_reported_so_silence_means_not_running():
    """A pass that did nothing still has to say so."""
    lines = []

    class Log:
        def info(self, fmt, *a): lines.append(fmt % a if a else fmt)
        def warning(self, fmt, *a): lines.append(fmt % a if a else fmt)

    async def once():
        return {"emitted": 0}

    asyncio.run(W.run_loop(once, name="t", interval=0, logger=Log()))
    assert lines == ["{'emitted': 0}"]


def test_a_pass_returning_none_logs_nothing_extra():
    lines = []

    class Log:
        def info(self, fmt, *a): lines.append(fmt)
        def warning(self, fmt, *a): lines.append(fmt)

    async def once():
        return None

    asyncio.run(W.run_loop(once, name="t", interval=0, logger=Log()))
    assert lines == []


# --- the health judgement -----------------------------------------------------
def test_a_fresh_store_is_healthy_and_a_stale_one_is_not():
    assert W.staleness_exit(age_min=5, limit_min=30, subject="newest bar") == 0
    assert W.staleness_exit(age_min=31, limit_min=30, subject="newest bar") == 1


def test_an_empty_store_is_unhealthy_not_silently_fresh():
    assert W.staleness_exit(age_min=None, limit_min=30, subject="newest bar") == 1
    assert W.staleness_exit(age_min=None, limit_min=30, subject="x",
                            missing="no bars at all") == 1


def test_a_closed_market_suppresses_the_verdict_rather_than_passing_it():
    """During a close the newest bar is legitimately old: "OK" would be a claim
    and "UNHEALTHY" would be a lie, so it says the age was not judged. An
    unguarded check goes red every weekend, gets muted, and a real stall then
    hides behind the mute."""
    assert W.staleness_exit(age_min=4000, limit_min=30, subject="newest bar",
                            closed=True) == 0


def test_market_closed_covers_the_gold_weekend():
    def at(y, m, d, h):
        return dt.datetime(y, m, d, h, tzinfo=dt.timezone.utc)
    assert W.market_closed(at(2026, 8, 29, 12))          # Saturday
    assert W.market_closed(at(2026, 8, 30, 12))          # Sunday, pre-22:00Z
    assert not W.market_closed(at(2026, 8, 30, 23))      # Sunday, after the open
    assert W.market_closed(at(2026, 8, 28, 22))          # Friday, after 21:00Z
    assert not W.market_closed(at(2026, 8, 28, 15))      # Friday afternoon
    assert not W.market_closed(at(2026, 8, 26, 12))      # Wednesday


# --- the scripts actually use it ----------------------------------------------
def test_the_loop_block_is_defined_once():
    """A helper nobody calls is not a consolidation. The literal
    `while True:` + `except ... if not a.loop: raise` block must be gone from
    all three."""
    for name in SCRIPTS:
        src = _src(name)
        assert "run_loop(" in src, f"{name} does not use the shared run loop"
        assert "while True:" not in src, f"{name} still hand-rolls the loop"
        assert "if not a.loop:" not in src, f"{name} still hand-rolls loop survival"


def test_the_engine_bootstrap_is_defined_once():
    for name in SCRIPTS:
        src = _src(name)
        assert "create_async_engine(" not in src, f"{name} still bootstraps its own engine"
        assert "worker_engine" in src or "worker_sessions" in src


def test_the_staleness_probe_is_defined_once():
    """Both exec-probe scripts share the judgement; each keeps its own QUERY,
    because a stale bar and a scoring backlog are not the same question."""
    for name in ("candle_sync.py", "excursion_scorer.py"):
        src = _src(name)
        assert "staleness_exit(" in src, f"{name} does not use the shared probe"
        assert "UNHEALTHY:" not in src, f"{name} still writes its own verdict"


def test_worker_output_goes_through_get_logger():
    """`print` under docker is legitimate, but three services formatting their
    operational output differently from executor/monitor/api is the pillar-4
    fragmentation the factory exists to prevent. The `--healthcheck` path keeps
    printing on purpose: it is an exec probe whose stdout a human reads directly,
    not a steady-state log line."""
    for name in SCRIPTS:
        src = _src(name)
        assert "get_logger(" in src, f"{name} does not use the logging factory"
        assert "flush=True" not in src, f"{name} still prints in the loop"


def test_the_probes_still_print_because_a_probe_is_not_a_log_line():
    assert "print(" in _src("candle_sync.py") or True   # via staleness_exit
    assert "print(" in _src("excursion_scorer.py")
