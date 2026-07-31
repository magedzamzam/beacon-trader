"""`generator:rules` — a strategy is JSON, not a deploy (#184).

WHY THERE IS ONE GENERATOR AND NOT ONE PER IDEA. The naive shape is
`generator:macd`, `generator:fvg`, `generator:order_block` — a Python function
each. That repeats exactly the mistake #167 fixed for filtration: before it,
gating on an indicator meant hand-writing an evaluator AND hand-plumbing a ctx
key, which does not scale to a 45-entry registry. `execution/strategy.py`
already owns a generic, registry-wide condition and `condition_requirements`
already says which instances to compute. Today that language answers "should I
filter this signal?"; evaluated bar-by-bar it answers "should I EMIT one?".
Same grammar, two uses — so adding a leaf type, or an indicator, benefits both.

WHAT IS GENUINELY NEW is `entry` / `sl` / `tps`. #169 §7: "Each generator must
define its own SL and TP ladder (that is what makes it a signal, and what R is
measured against)." A condition says WHEN; it does not say where the stop goes.
The vocabulary is deliberately small and extends the same way the conditions do.

    {
      "timeframe": "15m",
      "long":  {"when": {"all": [ ...conditions... ]}},
      "short": {"when": {...}},
      "entry": {"type": "close"},
      "sl":    {"type": "atr_mult", "timeframe": "1h", "period": 14, "mult": 1.5},
      "tps":   [{"type": "r_mult", "r": 1.0}, {"type": "r_mult", "r": 2.0}],
      "cooldown_bars": 60,
      "max_signals_per_day": 8,
      "trading_hours": { "sessions": [...] }        // optional, for session_in
    }

`cooldown_bars` and `max_signals_per_day` ARE NOT OPTIONAL EXTRAS. A condition
true for 50 consecutive bars emits 50 signals, each opening a position, and the
risk caps then decide the strategy — you would be measuring
`max_open_risk_per_symbol`, not the indicator. Both default to a non-zero value
here for that reason, and both report what they suppressed.

NO LOOK-AHEAD. A trigger bucket's condition is evaluated at that bucket's CLOSE
and sees only buckets that had fully closed by then — `ContextBuilder.
closed_bars` is the single place that boundary lives, and this module does not
re-implement it. The emitted signal is timestamped at that same close, so every
input predates the signal instant and nothing the signal reacts to is still in
progress. `tests/test_generators.py::test_a_generated_signal_saw_only_closed_bars`
asserts it against the frame rather than trusting the comment.

STILL NOT A ROUTE TO LIVE. §8 is blunt: searching (indicator x parameter x
threshold) against ~7 months of one instrument manufactures beautiful equity
curves that are pure hindsight. A validated generator does NOT go live from a
backtest — it needs the Lever-5 chain (a `kind='engine'` source, which does not
exist; a producer; shadow forward-R) and only then a weekend config act on one
arm. Backtest is the SCREENING step. Held-out is the only reportable result,
`n_variants_searched` counts the WHOLE grid, and N>=30 per variant still binds.

PURE — stdlib + beacon_core. No DB, no clock, no broker.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Sequence

from beacon_core.execution import strategy as ST
from beacon_core.execution.planner import validate_signal
from beacon_core.parsing.models import ParsedSignal
from beacon_core.trading_hours import sessions as TH

from . import bars as B
from .context import ContextBuilder
from .signal_sources import GeneratedSignal, GeneratorOutput, register_generator

NAME = "rules"
DEFAULT_TIMEFRAME = "15m"
DEFAULT_ATR_PERIOD = 14

# Non-zero on purpose: see the module docstring. A config that wants every bar
# has to say `"cooldown_bars": 0` out loud.
DEFAULT_COOLDOWN_BARS = 8
DEFAULT_MAX_PER_DAY = 8

# Bars of the trigger timeframe that must exist before the first evaluation, so
# a condition is not asked about a series too thin for its own indicators.
MIN_WARMUP_BARS = 30

ENTRY_TYPES = ("close", "level", "offset_atr")
SL_TYPES = ("atr_mult", "points", "level")
TP_TYPES = ("r_mult", "atr_mult", "points", "level")


class ConfigError(ValueError):
    """The config is unusable. Raised (not swallowed) because a generator that
    silently produced nothing would be indistinguishable from a strategy that
    never triggered — and the operator would read a flat equity curve as a
    finding."""


def _num(v, default=None) -> Optional[float]:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default: int) -> int:
    n = _num(v, None)
    return default if n is None else int(n)


def _sign(direction: str) -> int:
    """+1 for a BUY, -1 for a SELL — the direction 'better' points in."""
    return 1 if direction == "BUY" else -1


class RulesSpec:
    """The generator config, validated once. Every field it needs is resolved
    here so the per-bar loop is arithmetic, not parsing."""

    def __init__(self, config: dict):
        cfg = dict(config or {})
        self.timeframe = str(cfg.get("timeframe") or DEFAULT_TIMEFRAME)
        if B.timeframe_minutes(self.timeframe) is None:
            raise ConfigError(f"unknown timeframe {self.timeframe!r}")
        self.tf_minutes = B.timeframe_minutes(self.timeframe)
        self.symbol = str(cfg.get("symbol") or "XAUUSD")

        self.long = ((cfg.get("long") or {}).get("when")) or None
        self.short = ((cfg.get("short") or {}).get("when")) or None
        if self.long is None and self.short is None:
            raise ConfigError("a generator needs a `long.when` and/or a "
                              "`short.when` condition")

        self.entry = dict(cfg.get("entry") or {"type": "close"})
        self.sl = dict(cfg.get("sl") or {})
        self.tps = [dict(t) for t in (cfg.get("tps") or []) if isinstance(t, dict)]
        _check("entry", self.entry.get("type") or "close", ENTRY_TYPES)
        if not self.sl:
            raise ConfigError("a generator must define its own SL — that is what "
                              "R is measured against (#169 §7)")
        _check("sl", self.sl.get("type"), SL_TYPES)
        if not self.tps:
            raise ConfigError("a generator must define its own TP ladder")
        for t in self.tps:
            _check("tps[]", t.get("type"), TP_TYPES)

        self.cooldown_bars = max(0, _int(cfg.get("cooldown_bars"),
                                         DEFAULT_COOLDOWN_BARS))
        self.max_per_day = max(0, _int(cfg.get("max_signals_per_day"),
                                       DEFAULT_MAX_PER_DAY))
        self.sessions = ((cfg.get("trading_hours") or {}).get("sessions")) or None
        self.raw = cfg

    # --- what has to be computed, and only that ------------------------------
    def ta_rules(self) -> list:
        """The `[{"when": ...}]` list `ContextBuilder.ta_block` consumes.

        The GEOMETRY refs ride along as synthetic `indicator` leaves: a
        `{"type": "level", "id": "order_block", ...}` stop needs the same
        indicator instance computed as a condition on it would, and a level that
        was never computed would drop every signal for no visible reason."""
        rules = [{"when": c} for c in (self.long, self.short) if c]
        for ref in self._level_refs():
            rules.append({"when": {"type": "indicator", "id": ref.get("id"),
                                   "timeframe": ref.get("timeframe") or self.timeframe,
                                   "field": ref.get("field"),
                                   "params": ref.get("params"),
                                   "op": "eq", "value": 0}})
        return rules

    def _level_refs(self) -> list:
        out = []
        for spec in [self.entry, self.sl, *self.tps]:
            if (spec or {}).get("type") == "level" and spec.get("id"):
                out.append(spec)
        return out

    def digest_fields(self) -> dict:
        return {"timeframe": self.timeframe, "long": self.long,
                "short": self.short, "entry": self.entry, "sl": self.sl,
                "tps": self.tps, "cooldown_bars": self.cooldown_bars,
                "max_signals_per_day": self.max_per_day}


def _check(where: str, value, allowed) -> None:
    if value not in allowed:
        raise ConfigError(f"{where}: unknown type {value!r}; expected one of "
                          f"{', '.join(allowed)}")


# --- geometry resolution ------------------------------------------------------
# Every resolver returns None when an input is absent. None propagates: the
# signal is DROPPED and counted, never completed with a guessed price. A
# generator that invents a stop is not testing the strategy it claims to.
def _atr_of(builder: ContextBuilder, spec: dict, when: dt.datetime,
            default_tf: str) -> Optional[float]:
    tf = str(spec.get("timeframe") or default_tf)
    period = max(2, _int(spec.get("period"), DEFAULT_ATR_PERIOD))
    mult = _num(spec.get("mult"), None)
    if mult is None or mult <= 0:
        return None
    atr = builder.atr(tf, when, period)
    return None if atr is None else abs(atr) * mult


def _level_of(cond_ctx: dict, spec: dict) -> Optional[float]:
    return _num(ST.indicator_value(cond_ctx, spec), None)


def _entry_price(spec: RulesSpec, direction: str, close: float,
                 builder: ContextBuilder, when: dt.datetime,
                 cond_ctx: dict) -> Optional[float]:
    kind = spec.entry.get("type") or "close"
    if kind == "close":
        return close
    if kind == "level":
        lvl = _level_of(cond_ctx, spec.entry)
        if lvl is None:
            return None
        return lvl + _num(spec.entry.get("offset_points"), 0.0) * _sign(direction)
    # offset_atr: a pullback entry — BELOW the close for a BUY, above for a SELL.
    off = _atr_of(builder, spec.entry, when, spec.timeframe)
    return None if off is None else close - off * _sign(direction)


def _sl_price(spec: RulesSpec, direction: str, entry: float,
              builder: ContextBuilder, when: dt.datetime,
              cond_ctx: dict) -> Optional[float]:
    kind = spec.sl.get("type")
    sgn = _sign(direction)
    if kind == "atr_mult":
        d = _atr_of(builder, spec.sl, when, spec.timeframe)
        return None if d is None else entry - d * sgn
    if kind == "points":
        d = _num(spec.sl.get("points"), None)
        return None if d is None or d <= 0 else entry - abs(d) * sgn
    lvl = _level_of(cond_ctx, spec.sl)
    if lvl is None:
        return None
    # The buffer always pushes the stop FURTHER from entry — a stop placed
    # exactly on the level it is protecting is stopped out by the wick that
    # tests it.
    return lvl - abs(_num(spec.sl.get("buffer_points"), 0.0)) * sgn


def _tp_price(t: dict, direction: str, entry: float, risk: float,
              builder: ContextBuilder, when: dt.datetime, cond_ctx: dict,
              default_tf: str) -> Optional[float]:
    kind = t.get("type")
    sgn = _sign(direction)
    if kind == "r_mult":
        r = _num(t.get("r"), None)
        return None if r is None or r <= 0 else entry + risk * r * sgn
    if kind == "points":
        d = _num(t.get("points"), None)
        return None if d is None or d <= 0 else entry + abs(d) * sgn
    if kind == "atr_mult":
        d = _atr_of(builder, t, when, default_tf)
        return None if d is None else entry + d * sgn
    return _level_of(cond_ctx, t)


def _dec(v: float) -> Decimal:
    return Decimal(str(round(float(v), 5)))


def _build_signal(spec: RulesSpec, direction: str, close: float,
                  builder: ContextBuilder, when: dt.datetime,
                  cond_ctx: dict) -> tuple:
    """`(ParsedSignal, None)` or `(None, reason)`. The reason is a counter key,
    not a message — the counts are what the report shows."""
    entry = _entry_price(spec, direction, close, builder, when, cond_ctx)
    if entry is None:
        return None, "entry_unresolved"
    sl = _sl_price(spec, direction, entry, builder, when, cond_ctx)
    if sl is None:
        return None, "sl_unresolved"
    risk = abs(entry - sl)
    if risk <= 0:
        return None, "zero_risk"
    tps = []
    for t in spec.tps:
        px = _tp_price(t, direction, entry, risk, builder, when, cond_ctx,
                       spec.timeframe)
        if px is None:
            return None, "tp_unresolved"
        tps.append(px)
    # Ordered outward from entry, deduped: a ladder whose rungs are out of order
    # would have TP2 fill before TP1 and the R attribution would be nonsense.
    sgn = _sign(direction)
    tps = sorted(set(round(p, 5) for p in tps), key=lambda p: sgn * p)
    try:
        parsed = ParsedSignal(
            symbol=spec.symbol, direction=direction,
            entry_from=_dec(entry), entry_to=_dec(entry), sl=_dec(sl),
            tps=[_dec(p) for p in tps], order_type_hint=None,
            raw_text=f"generator:{NAME} {spec.timeframe} {direction} @ "
                     f"{when.isoformat()}")
    except (InvalidOperation, ValueError):
        return None, "unpriceable"
    ok, why = validate_signal(parsed)
    if not ok:
        return None, f"invalid_geometry:{why}"
    return parsed, None


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
    last_emit_index: Optional[int] = None
    per_day: Counter = Counter()

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

        long_v = (ST.evaluate_condition(spec.long, cond_ctx)
                  if spec.long else None)
        short_v = (ST.evaluate_condition(spec.short, cond_ctx)
                   if spec.short else None)
        if long_v is None and short_v is None:
            # Both sides UNKNOWN: an unknown indicator, a field the registry does
            # not emit, or a series still too thin. Counted, never emitted —
            # firing here would be trading on the absence of evidence.
            counts["n_unknown"] += 1
            continue
        if long_v is True and short_v is True:
            counts["n_both_sides_ambiguous"] += 1
            continue
        direction = "BUY" if long_v is True else "SELL" if short_v is True else None
        if direction is None:
            continue
        counts["n_triggered"] += 1

        if (spec.cooldown_bars and last_emit_index is not None
                and i - last_emit_index < spec.cooldown_bars):
            counts["n_suppressed_cooldown"] += 1
            continue
        day = when.date()
        if spec.max_per_day and per_day[day] >= spec.max_per_day:
            counts["n_suppressed_max_per_day"] += 1
            continue

        parsed, reason = _build_signal(spec, direction, bar.close, builder,
                                       when, cond_ctx)
        if parsed is None:
            counts["n_dropped_geometry"] += 1
            drops[reason] += 1
            continue

        out.append(GeneratedSignal(when, parsed))
        counts["n_emitted"] += 1
        counts[f"n_emitted_{direction}"] += 1
        per_day[day] += 1
        last_emit_index = i

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
