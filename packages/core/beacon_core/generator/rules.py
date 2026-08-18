"""The rules generator's brain, shared by the backtest and the live producer (#224).

Step 3 of the Lever-5 chain. The logic that turns a condition into a priced
signal used to live only in `services/replay/harness/generators.py`, which is
fine while the only caller is a backtest and fatal the moment a second one
exists: a live producer written against a COPY drifts from the harness on its
first edit, and every backtest number silently stops describing the thing that
trades. The one-way rule (CLAUDE.md) settles where it goes -- replay may import
`beacon_core`, never the reverse -- so the shared half moves here and replay
keeps the parts that are genuinely its own (bar iteration, resampling, its
`GeneratedSignal` wrapper).

WHAT IS SHARED: the config spec, the geometry (what entry/SL/TP resolve to), the
direction decision, and the caps. WHAT IS NOT: where the bars come from. A
backtest sweeps a resampled historical series; a producer is handed the latest
closed bar. Both then ask the same three questions in the same order, which is
what stops them disagreeing.

THE CONTEXT PROVIDER is the seam. Geometry needs one thing the condition context
cannot carry -- an ATR at an arbitrary timeframe -- so a caller passes any object
with `atr(timeframe, when, period) -> float | None`. Replay's `ContextBuilder`
already satisfies it; a live producer implements the same call over the TA
registry.

None PROPAGATES, everywhere. A geometry input that cannot be resolved drops the
signal and is COUNTED; it is never completed with a guessed level. A generator
that invents a stop is not testing the strategy it claims to.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Optional

from beacon_core.execution import strategy as ST
from beacon_core.execution.planner import validate_signal
from beacon_core.parsing.models import ParsedSignal
from beacon_core.trading_hours import sessions as TH

NAME = "rules"
DEFAULT_TIMEFRAME = "15m"
DEFAULT_ATR_PERIOD = 14

# Non-zero on purpose: a config that wants a signal on every bar has to say
# `"cooldown_bars": 0` out loud, because that is a strategy decision and not a
# default anyone should back into.
DEFAULT_COOLDOWN_BARS = 8
DEFAULT_MAX_PER_DAY = 8

# Bars of the trigger timeframe that must exist before the first evaluation, so
# a condition is not asked about a series too thin for its own indicators.
MIN_WARMUP_BARS = 30

ENTRY_TYPES = ("close", "level", "offset_atr")
SL_TYPES = ("atr_mult", "points", "level")
TP_TYPES = ("r_mult", "atr_mult", "points", "level")

TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240,
              "1d": 1440}


class ConfigError(ValueError):
    """The config is unusable. Raised, never swallowed: a generator that
    silently produced nothing is indistinguishable from a strategy that never
    triggered, and an operator would read that flat equity curve as a finding."""


def timeframe_minutes(tf) -> Optional[int]:
    return TF_MINUTES.get(str(tf))


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
    """+1 for a BUY, -1 for a SELL -- the direction 'better' points in."""
    return 1 if direction == "BUY" else -1


def _check(where: str, value, allowed) -> None:
    if value not in allowed:
        raise ConfigError("%s: unknown type %r; expected one of %s"
                          % (where, value, ", ".join(allowed)))


class RulesSpec:
    """The generator config, validated once, so the per-bar loop is arithmetic
    rather than parsing."""

    def __init__(self, config: dict):
        cfg = dict(config or {})
        self.timeframe = str(cfg.get("timeframe") or DEFAULT_TIMEFRAME)
        if timeframe_minutes(self.timeframe) is None:
            raise ConfigError("unknown timeframe %r" % self.timeframe)
        self.tf_minutes = timeframe_minutes(self.timeframe)
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
            raise ConfigError("a generator must define its own SL - that is what "
                              "R is measured against (#169 section 7)")
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
        """The `[{"when": ...}]` list a context builder consumes.

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


# --- geometry resolution -----------------------------------------------------
def _atr_of(provider, spec: dict, when: dt.datetime,
            default_tf: str) -> Optional[float]:
    tf = str(spec.get("timeframe") or default_tf)
    period = max(2, _int(spec.get("period"), DEFAULT_ATR_PERIOD))
    mult = _num(spec.get("mult"), None)
    if mult is None or mult <= 0:
        return None
    atr = provider.atr(tf, when, period)
    return None if atr is None else abs(atr) * mult


def _level_of(cond_ctx: dict, spec: dict) -> Optional[float]:
    return _num(ST.indicator_value(cond_ctx, spec), None)


def entry_price(spec: RulesSpec, direction: str, close: float, provider,
                when: dt.datetime, cond_ctx: dict) -> Optional[float]:
    kind = spec.entry.get("type") or "close"
    if kind == "close":
        return close
    if kind == "level":
        lvl = _level_of(cond_ctx, spec.entry)
        if lvl is None:
            return None
        return lvl + _num(spec.entry.get("offset_points"), 0.0) * _sign(direction)
    # offset_atr: a pullback entry -- BELOW the close for a BUY, above for a SELL.
    off = _atr_of(provider, spec.entry, when, spec.timeframe)
    return None if off is None else close - off * _sign(direction)


def sl_price(spec: RulesSpec, direction: str, entry: float, provider,
             when: dt.datetime, cond_ctx: dict) -> Optional[float]:
    kind = spec.sl.get("type")
    sgn = _sign(direction)
    if kind == "atr_mult":
        d = _atr_of(provider, spec.sl, when, spec.timeframe)
        return None if d is None else entry - d * sgn
    if kind == "points":
        d = _num(spec.sl.get("points"), None)
        return None if d is None or d <= 0 else entry - abs(d) * sgn
    lvl = _level_of(cond_ctx, spec.sl)
    if lvl is None:
        return None
    # The buffer always pushes the stop FURTHER from entry -- a stop placed
    # exactly on the level it is protecting is taken out by the wick that tests it.
    return lvl - abs(_num(spec.sl.get("buffer_points"), 0.0)) * sgn


def tp_price(t: dict, direction: str, entry: float, risk: float, provider,
             when: dt.datetime, cond_ctx: dict,
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
        d = _atr_of(provider, t, when, default_tf)
        return None if d is None else entry + d * sgn
    return _level_of(cond_ctx, t)


def _dec(v: float) -> Decimal:
    return Decimal(str(round(float(v), 5)))


def build_signal(spec: RulesSpec, direction: str, close: float, provider,
                 when: dt.datetime, cond_ctx: dict) -> tuple:
    """`(ParsedSignal, None)` or `(None, reason)`. The reason is a counter key,
    not a message -- the counts are what the report shows."""
    entry = entry_price(spec, direction, close, provider, when, cond_ctx)
    if entry is None:
        return None, "entry_unresolved"
    sl = sl_price(spec, direction, entry, provider, when, cond_ctx)
    if sl is None:
        return None, "sl_unresolved"
    risk = abs(entry - sl)
    if risk <= 0:
        return None, "zero_risk"
    tps = []
    for t in spec.tps:
        px = tp_price(t, direction, entry, risk, provider, when, cond_ctx,
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
            raw_text="generator:%s %s %s @ %s" % (NAME, spec.timeframe, direction,
                                                  when.isoformat()))
    except (InvalidOperation, ValueError):
        return None, "unpriceable"
    ok, why = validate_signal(parsed)
    if not ok:
        return None, "invalid_geometry:%s" % why
    return parsed, None


def condition_context(spec: "RulesSpec", provider, close: float,
                      when: dt.datetime) -> dict:
    """Everything the conditions are allowed to read at `when`.

    Shared for the same reason the rest of this module is: the backtest and the
    producer must ask the conditions about the SAME inputs, and a context built
    twice diverges the first time one of them learns a new block. Only what a
    rule actually references is computed -- nothing is fetched for a rule set
    that mentions no TA.

    `provider` needs `ta_block(rules, when)` and `adx_block(tf, when)` on top of
    `atr(...)`; replay's ContextBuilder has all three."""
    ctx: dict = {"price": close}
    ta = provider.ta_block(spec.ta_rules(), when)
    if ta:
        ctx["ta"] = ta
    adx = {}
    for tf in sorted(ST.adx_rule_timeframes(
            [{"when": c} for c in (spec.long, spec.short) if c])):
        blk = provider.adx_block(tf, when)
        if blk is not None:
            adx[tf] = blk
    if adx:
        ctx["adx"] = adx
    if spec.sessions:
        try:
            ctx["sessions"] = list(
                (TH.status(spec.sessions, when) or {}).get("active") or [])
        except Exception:
            pass                              # fail-open, exactly as live
    return ctx


# --- the decision, in the order both callers must ask it ---------------------
def decide_direction(spec: RulesSpec, cond_ctx: dict) -> tuple:
    """`(direction | None, counter_key | None)`.

    UNKNOWN is not False. Both sides unknown means an unknown indicator, a field
    the registry does not emit, or a series still too thin -- counted and never
    emitted, because firing there would be trading on the absence of evidence.
    Both sides TRUE is ambiguous and is also refused rather than guessed."""
    long_v = ST.evaluate_condition(spec.long, cond_ctx) if spec.long else None
    short_v = ST.evaluate_condition(spec.short, cond_ctx) if spec.short else None
    if long_v is None and short_v is None:
        return None, "n_unknown"
    if long_v is True and short_v is True:
        return None, "n_both_sides_ambiguous"
    if long_v is True:
        return "BUY", None
    if short_v is True:
        return "SELL", None
    return None, None


class CapState:
    """Cooldown and per-day caps, held outside the loop so a live producer
    suppresses exactly as the backtest did.

    These are NOT optional extras. A condition true for 50 consecutive bars
    emits 50 signals, each opening a position, and `max_open_risk_per_symbol`
    then decides the strategy -- you would be measuring the risk caps rather
    than the indicator (#169)."""

    def __init__(self, spec: RulesSpec):
        self.cooldown_bars = spec.cooldown_bars
        self.max_per_day = spec.max_per_day
        self.last_emit_index: Optional[int] = None
        self.per_day: dict = {}

    def suppressed(self, index: int, when: dt.datetime) -> Optional[str]:
        """The counter key for why this trigger is suppressed, or None."""
        if (self.cooldown_bars and self.last_emit_index is not None
                and index - self.last_emit_index < self.cooldown_bars):
            return "n_suppressed_cooldown"
        day = when.date()
        if self.max_per_day and self.per_day.get(day, 0) >= self.max_per_day:
            return "n_suppressed_max_per_day"
        return None

    def record(self, index: int, when: dt.datetime) -> None:
        day = when.date()
        self.per_day[day] = self.per_day.get(day, 0) + 1
        self.last_emit_index = index
