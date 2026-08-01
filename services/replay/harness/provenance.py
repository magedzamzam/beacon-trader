"""Which signals are the account's own past, and which are imported history (#192).

`signals.created_at` is INGEST time, not the time the signal was issued. When a
channel is onboarded its whole backlog arrives at once, so a block of rows share
a single moment. Measured on the current book: 230 of 856 signals sit in a
15-minute window holding 10 or more, and NONE of the 179 in the biggest one ever
produced a trade.

That makes two different things wrong at once, and they need different remedies:

  * **P&L.** Replaying a signal the account never had the chance to take and
    reporting the result as the book's past is simply the wrong number. The
    remedy is exclusion — `signals.backfilled` marks the rows, and the loaders
    that feed a P&L report drop them by default.
  * **Indicator context.** Every signal in a burst resolves against the SAME
    15m bar, so a filter can only keep or drop the whole block: its effect there
    is *unmeasured*, not small. The remedy is disclosure, because the underlying
    time is gone and guessing it is worse than saying so.

The burst detector below is the disclosure half. It is a heuristic over
timestamps and stays useful after the flag ships: it catches the NEXT onboarding
before anyone has marked it. `backfilled` is the authoritative half. Keeping
both means an unmarked burst is still declared rather than silently priced in.

PURE — stdlib only, no DB, no `beacon_core`.
"""
from __future__ import annotations

from typing import Iterable, List

# A burst is >= BULK_MIN_SIGNALS signals sharing one BULK_BAR_MINUTES window.
# 15m because that is the timeframe the shipped entry filters read, so "one
# window" is literally "one bar the filter can see"; 10 because a real book's
# busiest 15 minutes holds 9 trades, so 10+ is bulk import, not a busy session.
BULK_BAR_MINUTES = 15
BULK_MIN_SIGNALS = 10


def bar_key(at, *, bar_minutes: int = BULK_BAR_MINUTES):
    """The start of the `bar_minutes` window `at` falls in."""
    return at.replace(minute=(at.minute // bar_minutes) * bar_minutes,
                      second=0, microsecond=0)


def bursts(signals: Iterable, *, bar_minutes: int = BULK_BAR_MINUTES,
           min_signals: int = BULK_MIN_SIGNALS) -> dict:
    """`{window_start: count}` for every window holding `min_signals` or more.

    Rows with an unusable timestamp are skipped rather than raised on: this
    feeds a caveat line, and a caveat that can crash a report is worse than a
    caveat that misses a row."""
    counts: dict = {}
    for s in signals:
        try:
            k = bar_key(s.at, bar_minutes=bar_minutes)
        except (AttributeError, TypeError, ValueError):
            continue
        counts[k] = counts.get(k, 0) + 1
    return {k: n for k, n in counts.items() if n >= min_signals}


def is_backfilled(row) -> bool:
    """True only when the row says so. A source that predates the flag reads as
    live — the same direction as the DB default, so the two never disagree."""
    return bool(getattr(row, "backfilled", False))


def live_only(signals: Iterable) -> List:
    """The signals the account could actually have traded."""
    return [s for s in signals if not is_backfilled(s)]


def pnl_set(signals: Iterable, *, include_backfilled: bool = False) -> List:
    """The signal set a P&L report is allowed to use.

    One function so the rule lives in one place: a caller either takes the
    live-only default or opts in explicitly, and there is no third way to end up
    with imported history in a money number."""
    return list(signals) if include_backfilled else live_only(signals)
