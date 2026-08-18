"""`generator:rules` — the harness half of the shared rules generator (#184/#224).

THE BRAIN MOVED. The config spec, the geometry (what entry/SL/TP resolve to),
the direction decision and the caps now live in
`beacon_core.generator.rules`, because a live producer is about to become a
second caller and a producer written against a COPY drifts from the harness on
its first edit -- at which point every backtest number silently stops describing
the thing that trades. The one-way rule settles the direction: replay may import
`beacon_core`, never the reverse.

WHAT STAYS HERE is what is genuinely the backtest's own: where bars come from.
This module resamples a historical series to the trigger timeframe, builds the
condition context per bar, and wraps the result in the harness's
`GeneratedSignal`. A live producer will do the first and third differently and
the middle identically, which is the point.

`ContextBuilder` is the context provider the shared geometry expects -- anything
with `atr(timeframe, when, period)`.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import List, Optional, Sequence

from beacon_core.execution import strategy as ST
from beacon_core.generator import rules as G
# Re-exported so the harness's public surface is unchanged by the move: callers
# and tests that referenced `harness.generators.X` keep working, and there is
# exactly one definition of each.
from beacon_core.generator.rules import (
    DEFAULT_ATR_PERIOD, DEFAULT_COOLDOWN_BARS, DEFAULT_MAX_PER_DAY,
    DEFAULT_TIMEFRAME, ENTRY_TYPES, MIN_WARMUP_BARS, NAME, SL_TYPES, TP_TYPES,
    ConfigError, RulesSpec)
from beacon_core.trading_hours import sessions as TH

from . import bars as B
from .context import ContextBuilder
from .signal_sources import GeneratedSignal, GeneratorOutput, register_generator


# --- the generator ------------------------------------------------------------
def rules_generator(bars: Sequence[B.Bar], config: dict) -> GeneratorOutput:
    """Scan the trigger timeframe and emit a signal wherever the config says so.

    Returns a `GeneratorOutput` — a list of `GeneratedSignal`, carrying `.stats`
    with what it suppressed and dropped. Those counts are the honest half of the
    result: a run that emitted 40 signals having suppressed 900 by cooldown is a
    different strategy from one that triggered 40 times."""
    spec = RulesSpec(config)
    series = B.BarSeries(list(bars))
    builder = ContextBuilder(series)
    frame = B.resample(series.bars, spec.timeframe)
    ta_rules = spec.ta_rules()
    adx_tfs = sorted(ST.adx_rule_timeframes(
        [{"when": c} for c in (spec.long, spec.short) if c]))

    # Seeded at zero, not left to `Counter` to invent on first use: a stats block
    # where `n_emitted` is ABSENT rather than 0 reads as "not reported" — and the
    # suppression counts are exactly the ones a reader is most likely to skip if
    # they have to notice a missing key to find them.
    counts: Counter = Counter({k: 0 for k in (
        "n_bars_evaluated", "n_unknown", "n_both_sides_ambiguous", "n_triggered",
        "n_suppressed_cooldown", "n_suppressed_max_per_day", "n_dropped_geometry",
        "n_emitted", "n_emitted_BUY", "n_emitted_SELL")})
    drops: Counter = Counter()
    out: List[GeneratedSignal] = []
    caps = G.CapState(spec)

    for i in range(MIN_WARMUP_BARS, len(frame)):
        bar = frame[i]
        # The instant the bucket CLOSED. Everything the condition reads has
        # closed by then, and the signal is stamped here — see the module
        # docstring. `closed_bars` owns the boundary; this is just its argument.
        when = bar.ts + dt.timedelta(minutes=spec.tf_minutes)
        counts["n_bars_evaluated"] += 1

        cond_ctx: dict = {"price": bar.close}
        ta = builder.ta_block(ta_rules, when)
        if ta:
            cond_ctx["ta"] = ta
        adx = {}
        for tf in adx_tfs:
            blk = builder.adx_block(tf, when)
            if blk is not None:
                adx[tf] = blk
        if adx:
            cond_ctx["adx"] = adx
        if spec.sessions:
            try:
                cond_ctx["sessions"] = list(
                    (TH.status(spec.sessions, when) or {}).get("active") or [])
            except Exception:
                pass                              # fail-open, exactly as live

        direction, why = G.decide_direction(spec, cond_ctx)
        if direction is None:
            if why:
                counts[why] += 1
            continue
        counts["n_triggered"] += 1

        suppressed = caps.suppressed(i, when)
        if suppressed:
            counts[suppressed] += 1
            continue

        parsed, reason = G.build_signal(spec, direction, bar.close, builder,
                                        when, cond_ctx)
        if parsed is None:
            counts["n_dropped_geometry"] += 1
            drops[reason] += 1
            continue

        out.append(GeneratedSignal(when, parsed))
        counts["n_emitted"] += 1
        counts[f"n_emitted_{direction}"] += 1
        caps.record(i, when)

    stats = {
        **{k: int(v) for k, v in sorted(counts.items())},
        "timeframe": spec.timeframe,
        "n_frame_bars": len(frame),
        "warmup_bars_skipped": min(MIN_WARMUP_BARS, len(frame)),
        "cooldown_bars": spec.cooldown_bars,
        "max_signals_per_day": spec.max_per_day,
        "dropped_geometry_breakdown": {k: int(v) for k, v in sorted(drops.items())},
        "config": spec.digest_fields(),
        "note": ("Suppression and drop counts are part of the result, not a "
                 "footnote: N signals emitted after M cooldown suppressions is a "
                 "different strategy from one that triggered N times. A dropped "
                 "geometry is a bar the condition fired on and the ladder could "
                 "not be priced — it is NOT a trade that lost."),
        "not_a_route_to_live": (
            "A validated generator does not go live from a backtest. It needs the "
            "Lever-5 chain — a kind='engine' source (which does not exist today), "
            "a producer, and shadow forward-R — and only then a weekend config act "
            "on one arm. Held-out is the only reportable result; n_variants_searched "
            "counts the whole grid; N>=30 per variant still binds (#169 §8)."),
    }
    return GeneratorOutput(out, stats)


register_generator(NAME, rules_generator)
