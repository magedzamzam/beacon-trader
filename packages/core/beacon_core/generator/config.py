"""What a valid engine strategy looks like, checked BEFORE it is saved (#224).

The operator requirement for Lever 5 is that a strategy can be added, edited and
changed from the portal with no code change and no redeploy. That is already
possible - #184 made the condition grammar JSON and shared it between filtration
and generation - but "editable" without "validated" is a trap: the grammar is
FAIL-OPEN by design (a leaf whose input is missing is UNKNOWN, never True), so a
strategy with a typo does not error. It silently generates nothing, forever, and
the only symptom is an engine that never fires.

So the save path checks the config here, and the same function is what a
producer asserts on startup. Pure and dependency-light: the API turns these
strings into 422s, nothing here imports FastAPI.

An engine strategy lives on its own SOURCE row rather than in a new table -
`sources` already carries `kind`, a `strategy` JSON blob, `account_map`,
`enabled_for_trading` and `is_trusted`, which is every knob an engine needs plus
the screens that already manage them:

    sources.kind                = 'engine'
    sources.strategy.generator  = {...this config...}
    sources.enabled_for_trading = false          # inert until a weekend act
"""
from __future__ import annotations

from beacon_core.ta import registry as TA

KIND_TELEGRAM, KIND_TRADINGVIEW = "telegram", "tradingview"
KIND_MANUAL, KIND_API, KIND_ENGINE = "manual", "api", "engine"

# The vocabulary lived only in a comment on the model. One home for it, so the
# API and the producer cannot disagree about what a source may be.
SOURCE_KINDS = (KIND_TELEGRAM, KIND_TRADINGVIEW, KIND_MANUAL, KIND_API, KIND_ENGINE)

_ENTRY_TYPES = {"close", "level", "offset_atr"}
_SL_TYPES = {"atr_mult", "points", "level"}
_TP_TYPES = {"r_mult", "atr_mult", "points", "level"}
_COMPOSERS = ("all", "any", "not")
# Ops the indicator evaluator implements (execution/strategy._NUM_OPS plus the
# boolean ones).
_OPS = {"gt", "gte", "lt", "lte", "eq", "ne", "between", "outside",
        "is_true", "is_false"}
_MAX_SIGNALS_CEILING = 200        # a sanity bound, not a strategy opinion


def _check_when(when, where, out, depth=0):
    """One condition node: a composer, or an indicator leaf."""
    if depth > 8:
        out.append("%s: condition nests too deeply" % where)
        return
    if not isinstance(when, dict) or not when:
        out.append("%s: condition must be a non-empty object" % where)
        return
    for comp in _COMPOSERS:
        if comp in when:
            body = when[comp]
            if comp == "not":
                _check_when(body, "%s.not" % where, out, depth + 1)
            elif isinstance(body, list) and body:
                for i, sub in enumerate(body):
                    _check_when(sub, "%s.%s[%d]" % (where, comp, i), out, depth + 1)
            else:
                out.append("%s.%s: must be a non-empty list" % (where, comp))
            return
    if when.get("type") != "indicator":
        out.append("%s: a generator condition must be an `indicator` leaf or "
                   "all/any/not - got %r. Session and regime leaves gate an "
                   "existing signal; they cannot create one"
                   % (where, when.get("type")))
        return
    inst = TA.resolve_instance(when.get("id"), when.get("params"))
    if inst is None:
        known = ", ".join(sorted(s["id"] for s in TA.REGISTRY))
        out.append("%s: unknown indicator %r (known: %s)"
                   % (where, when.get("id"), known))
        return
    tf = when.get("timeframe")
    if tf and tf not in TA.AVAILABLE_TIMEFRAMES:
        out.append("%s: unknown timeframe %r (known: %s)"
                   % (where, tf, ", ".join(TA.AVAILABLE_TIMEFRAMES)))
    field, outputs = when.get("field"), (inst.get("outputs") or [])
    if field and outputs and field not in outputs:
        out.append("%s: %r has no field %r (has: %s)"
                   % (where, inst["id"], field, ", ".join(outputs)))
    op = when.get("op")
    if op not in _OPS:
        out.append("%s: unknown op %r (known: %s)"
                   % (where, op, ", ".join(sorted(_OPS))))
    elif (op not in ("is_true", "is_false")
            and when.get("value") is None and when.get("ref") is None):
        out.append("%s: op %r needs a `value` or a `ref`" % (where, op))


def _check_geometry(cfg, out):
    """What makes it a SIGNAL rather than a condition (#169 section 7)."""
    entry = cfg.get("entry")
    if not isinstance(entry, dict) or entry.get("type") not in _ENTRY_TYPES:
        out.append("entry.type must be one of %s" % sorted(_ENTRY_TYPES))
    sl = cfg.get("sl")
    if not isinstance(sl, dict) or sl.get("type") not in _SL_TYPES:
        out.append("sl.type must be one of %s - a signal with no stop cannot be "
                   "sized, and R is measured against it" % sorted(_SL_TYPES))
    elif sl.get("type") == "atr_mult":
        try:
            if float(sl.get("mult")) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            out.append("sl.mult must be a positive number")
    tps = cfg.get("tps")
    if not isinstance(tps, list) or not tps:
        out.append("tps must be a non-empty list - a signal with no target "
                   "never exits")
        return
    for i, tp in enumerate(tps):
        if not isinstance(tp, dict) or tp.get("type") not in _TP_TYPES:
            out.append("tps[%d].type must be one of %s" % (i, sorted(_TP_TYPES)))
    r_vals = [tp.get("r") for tp in tps
              if isinstance(tp, dict) and tp.get("type") == "r_mult"]
    if r_vals and any(v is None for v in r_vals):
        out.append("an r_mult target needs `r`")
    else:
        nums = [float(v) for v in r_vals if v is not None]
        if nums != sorted(nums):
            out.append("tps must be ordered outward - TP2 nearer than TP1 books "
                       "the ladder in the wrong order")


def _check_caps(cfg, out):
    """NOT optional, and this is the reason (#169).

    A condition true for 50 consecutive bars emits 50 signals, each opening a
    position, and the risk caps then decide the strategy - you would be
    measuring `max_open_risk_per_symbol`, not the indicator."""
    for key, lo, hi in (("cooldown_bars", 1, 10000),
                        ("max_signals_per_day", 1, _MAX_SIGNALS_CEILING)):
        v = cfg.get(key)
        if v is None:
            out.append("%s is required - without it one persistent condition "
                       "emits a signal every bar and the risk caps become the "
                       "strategy" % key)
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            out.append("%s must be a whole number" % key)
            continue
        if not lo <= n <= hi:
            out.append("%s must be between %d and %d" % (key, lo, hi))


def validate_generator_config(cfg):
    """Every problem with an engine strategy, as human-readable strings.

    Returns [] when the config is one the producer can actually run. Collects
    ALL errors rather than raising on the first: the portal shows them together
    instead of making the operator save five times to find five typos."""
    out = []
    if not isinstance(cfg, dict) or not cfg:
        return ["generator config must be a non-empty object"]

    tf = cfg.get("timeframe")
    if not tf:
        out.append("timeframe is required - it is the bar the conditions are "
                   "read on")
    elif tf not in TA.AVAILABLE_TIMEFRAMES:
        out.append("unknown timeframe %r (known: %s)"
                   % (tf, ", ".join(TA.AVAILABLE_TIMEFRAMES)))

    sides = [s for s in ("long", "short") if cfg.get(s)]
    if not sides:
        out.append("at least one of `long` / `short` must define a condition, "
                   "or the strategy can never emit a signal")
    for side in sides:
        block = cfg.get(side)
        if not isinstance(block, dict) or "when" not in block:
            out.append("%s must be an object with a `when` condition" % side)
            continue
        _check_when(block["when"], side, out)

    _check_geometry(cfg, out)
    _check_caps(cfg, out)
    return out


def engine_config(source_strategy):
    """The generator block off a source's `strategy`, or {} when absent."""
    if not isinstance(source_strategy, dict):
        return {}
    cfg = source_strategy.get("generator")
    return cfg if isinstance(cfg, dict) else {}
