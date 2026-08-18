"""Emitting engine signals into the ledger WITHOUT trading them (#224, step 4).

The point of Lever 5 is not to trade a generated signal. It is to find out
whether generated signals are worth trading, and that needs N>=30 of them scored
forward. At ~1.1 signals/day that is about six weeks, so the shadow has to start
long before any decision is due.

THE SCORING ALREADY EXISTS AND IS NOT REBUILT HERE. `analysis/excursion_store`
computes a `BASIS_SIGNAL` excursion for every signal from the 1m bars, using the
signal's own stated entry and `trade_id=None` -- it deliberately covers signals
that were skipped, filtered, blocked or never filled. So a signal row is all the
forward-R machinery needs. This module's whole job is to write that row.

WHY THIS CANNOT PLACE AN ORDER, structurally rather than by a flag. A signal
reaches the executor exactly once, through `bus.enqueue(CH_SIGNAL_VALID, ...)`
in `ingest/pipeline.py`. This module has no bus import and no enqueue call, and
a test asserts it stays that way. The `enabled_for_trading=false` on the source
is a second, independent guard -- but the first one is that nothing here can
reach the queue at all.

CAP STATE COMES FROM THE LEDGER, not from memory. A producer restarts; a
`CapState` held in a process does not survive that, and the cooldown that exists
to stop a persistent condition emitting every bar would reset with it. So the
cooldown and the daily cap are derived from the signals already written for the
source, which is the only record that outlives the process.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from beacon_core.generator import rules as G


def latest_closed_bar(frame, now: dt.datetime, tf_minutes: int):
    """The newest bucket that has fully CLOSED, and its close time.

    A bucket whose window still contains `now` is not closed: reading it would
    ask a condition about a high that has not printed yet, and the backtest --
    which only ever sees complete buckets -- would never have seen it. Returns
    `(index, bar, closed_at)` or `(None, None, None)`."""
    step = dt.timedelta(minutes=tf_minutes)
    for i in range(len(frame) - 1, -1, -1):
        closed_at = frame[i].ts + step
        if closed_at <= now:
            return i, frame[i], closed_at
    return None, None, None


def suppressed_by_ledger(spec: G.RulesSpec, closed_at: dt.datetime,
                         last_signal_at: Optional[dt.datetime],
                         count_today: int) -> Optional[str]:
    """The caps, evaluated against what is already in the ledger.

    Same two rules the backtest applies, expressed in time rather than bar index
    because that is what survives a restart: `cooldown_bars` becomes a duration
    at the trigger timeframe."""
    if spec.max_per_day and count_today >= spec.max_per_day:
        return "n_suppressed_max_per_day"
    if spec.cooldown_bars and last_signal_at is not None:
        gap = (closed_at - last_signal_at).total_seconds() / 60.0
        if gap < spec.cooldown_bars * spec.tf_minutes:
            return "n_suppressed_cooldown"
    return None


def evaluate_latest(spec: G.RulesSpec, provider, frame, cond_ctx: dict,
                    now: dt.datetime, *, last_signal_at=None,
                    count_today: int = 0) -> dict:
    """Decide whether the newest closed bar produces a signal.

    Returns a dict that always says what happened, because "nothing emitted" has
    several very different causes and a producer that cannot tell them apart is
    one nobody can debug: `{"signal", "closed_at", "direction", "reason"}`."""
    idx, bar, closed_at = latest_closed_bar(frame, now, spec.tf_minutes)
    if bar is None:
        return {"signal": None, "closed_at": None, "direction": None,
                "reason": "no_closed_bar"}
    if idx < G.MIN_WARMUP_BARS:
        return {"signal": None, "closed_at": closed_at, "direction": None,
                "reason": "warmup"}

    direction, why = G.decide_direction(spec, cond_ctx)
    if direction is None:
        return {"signal": None, "closed_at": closed_at, "direction": None,
                "reason": why or "no_trigger"}

    capped = suppressed_by_ledger(spec, closed_at, last_signal_at, count_today)
    if capped:
        return {"signal": None, "closed_at": closed_at, "direction": direction,
                "reason": capped}

    parsed, drop = G.build_signal(spec, direction, bar.close, provider,
                                  closed_at, cond_ctx)
    if parsed is None:
        return {"signal": None, "closed_at": closed_at, "direction": direction,
                "reason": "dropped_geometry:%s" % drop}
    return {"signal": parsed, "closed_at": closed_at, "direction": direction,
            "reason": None}


def shadow_signal_row(parsed, source_id: int, closed_at: dt.datetime) -> dict:
    """The `signals` row for a generated signal, as a plain dict.

    `status` is `shadow`: it is not `executed` (nothing traded), not `rejected`
    (nothing was wrong with it) and not `skipped` (no rule declined it). A
    distinct status is what lets every existing per-source rollup exclude engine
    rows until the day they are meant to be included -- and lets the weekly
    count them separately without inferring intent from a source kind.

    `signal_at` is the bar's CLOSE, not the write time: forward R is measured
    from the moment the condition became true, and a producer that ran late
    would otherwise score itself from whenever it happened to wake up."""
    return {
        "source_id": source_id,
        "symbol": parsed.symbol,
        "direction": parsed.direction,
        "entry_from": parsed.entry_from,
        "entry_to": parsed.entry_to,
        "sl": parsed.sl,
        "tps": [str(t) for t in parsed.tps],
        "order_type": None,
        "status": "shadow",
        "raw_text": parsed.raw_text,
        "signal_at": closed_at,
    }
