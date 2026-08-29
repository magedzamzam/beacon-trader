"""Shared scaffolding for the persistent worker scripts (#258).

The Lever-5 chain added three long-running processes that now ship as compose
services — `candle-sync`, `engine-producer`, `excursion-scorer` — and each
started from a copy of the last. Three near-verbatim `while True / try / except /
sleep` blocks (the comments even said so: *"same fix as candle-sync"*, *"same rot
candle-sync had"*), two identical engine bootstraps, two copy-pasted staleness
probes, and 29 `print(..., flush=True)` calls against zero uses of the
`get_logger` factory the executor, monitor and api all standardise on.

Nothing here changes what any worker does. It exists because the pattern landed
three times in one cycle and will land again, and loop-survival semantics, health
philosophy and log format should not drift apart across the fleet.

THE HEALTH PHILOSOPHY IS THE PART WORTH KEEPING. These workers probe the STORE,
not the process: `candle-sync` asks whether the newest bar is recent, not whether
its own PID is alive. A liveness probe would have reported green through the
entire 15-day stall that #224 is about — because nothing was running at all.
`staleness_exit` is that judgement, written once.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Optional

from .logging import get_logger


@asynccontextmanager
async def worker_engine(url: Optional[str] = None):
    """An async engine for the run, disposed on the way out.

    `DATABASE_URL` by default, because that is what compose gives every one of
    these containers and a worker that invents its own source of truth for the
    connection string is a worker that can point at the wrong database."""
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(url or os.environ["DATABASE_URL"])
    try:
        yield eng
    finally:
        await eng.dispose()


@asynccontextmanager
async def worker_sessions(url: Optional[str] = None):
    """`(engine, session_factory)`, disposed on the way out.

    `expire_on_commit=False` matches what all three workers already did: a pass
    reads its own writes back after committing, and re-loading them costs a
    round trip for nothing."""
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import sessionmaker

    async with worker_engine(url) as eng:
        yield eng, sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)


async def run_loop(run_once: Callable[[], Awaitable], *, name: str,
                   interval: int = 0, logger=None) -> None:
    """Run `run_once` forever every `interval` seconds, or exactly once.

    THE TWO BEHAVIOURS THAT MUST NOT DRIFT, and the reason this is one function:

      * `interval == 0` is ONE pass and then return — every existing manual
        invocation of these scripts relies on it, and a failure there RAISES so
        an operator running a catch-up by hand sees it and the exit code is
        non-zero.
      * under `--loop` a bad pass is logged and the loop CONTINUES. A worker that
        dies on one malformed window is a worker that stops syncing until someone
        notices, which is the failure mode all three of these exist to end.

    `run_once` returns whatever it likes; a non-None result is logged, so a pass
    that did nothing still says so and silence means the worker is not running.
    """
    log = logger or get_logger(name)
    while True:
        try:
            result = await run_once()
            if result is not None:
                log.info("%s", result)
        except Exception as exc:            # a bad pass must not kill the loop
            if not interval:
                raise
            log.warning("pass failed: %s", exc)
        if not interval:
            return
        log.info("sleeping %ds", interval)
        await asyncio.sleep(interval)


# --- health: probe the STORE, not the process ---------------------------------
def market_closed(now: Optional[dt.datetime] = None) -> bool:
    """Is the gold market shut right now?

    Weekends are the trap a naive age check falls into: gold prints no bars from
    Friday 21:00Z to Sunday 22:00Z, so an unguarded probe goes red every weekend,
    gets muted, and a real stall then hides behind the mute."""
    now = now or dt.datetime.now(dt.timezone.utc)
    wd, hour = now.weekday(), now.hour
    return wd == 5 or (wd == 6 and hour < 22) or (wd == 4 and hour >= 21)


def staleness_exit(*, age_min: Optional[float], limit_min: float, subject: str,
                   closed: bool = False, missing: Optional[str] = None) -> int:
    """The `--healthcheck` decision and its vocabulary, in one place.

    Returns the process exit code (0 healthy, 1 stale) and prints the one line a
    `docker compose ps` reader gets. Callers supply the QUERY — what "stale"
    means differs per store (newest bar vs oldest unscored signal) — and this
    supplies the judgement, so the three probes cannot answer the same question
    in three different ways.

    `closed` suppresses the verdict rather than passing it: during a market
    close the newest bar is legitimately old and staleness is not measurable, so
    saying "OK" would be a claim and saying "UNHEALTHY" would be a lie. It says
    the age was not judged.
    """
    if missing:
        print("UNHEALTHY: %s" % missing)
        return 1
    if age_min is None:
        print("UNHEALTHY: %s has no age to check" % subject)
        return 1
    if closed:
        print("OK (market closed): %s, age %.0f min not judged" % (subject, age_min))
        return 0
    if age_min > limit_min:
        print("UNHEALTHY: %s is %.0f min old (limit %g)" % (subject, age_min, limit_min))
        return 1
    print("OK: %s, age %.0f min" % (subject, age_min))
    return 0
