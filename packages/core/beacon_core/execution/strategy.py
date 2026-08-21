"""Per-(account, source) execution-strategy resolution (#84).

An ExecutionStrategy carries three pillars — entry_policy / entry_filters /
exit_policy — scoped by (account_id, source_id), either nullable. This module
resolves the MOST-SPECIFIC enabled strategy for a trade and exposes per-pillar
getters that fall back to the global/source default when a pillar is absent, so
'no strategy configured' is byte-identical to today.

Pure and dependency-light (only the built-in SL default is imported) so it runs
on a bare box and is safe from both the executor (snapshot at entry) and monitor.
"""
from __future__ import annotations

import datetime as _dt
from typing import NamedTuple

from ..strategy.rules import DEFAULT_SL_RULES
from ..ta import registry as TA

# The entry-policy keys the planner/executor understand (chase guard #67 + TTL).
# entry_style + staged drive the confirmation-staged entry model (#129); staged is
# a nested block validated by execution.staging.clean_staged_config.
# `entry_policy` merges key-by-key over THIS TUPLE, so a key missing from it is
# dropped on the way through and the setting silently appears to do nothing.
# sl_distance (#249) replaces the channel's stop with one this far from the entry.
# NOT here: the staged-entry ladder. It is a GLOBAL grid (zone shape x TP count)
# in the `staged_ladders` setting, because which ladder a signal runs follows
# from the signal's own shape, not from the channel. A strategy only chooses
# whether staged entry is on, via `entry_style`.
ENTRY_POLICY_KEYS = ("ttl_minutes", "honor_market_hint", "chase_tolerance_r",
                     "chase_tolerance_atr", "beyond_tolerance", "max_tp_distance_pct",
                     "entry_style", "staged", "sl_distance")


def resolve_chain(strategies, account_id, source_id) -> list:
    """Every ENABLED strategy whose scope matches (account, source), MOST-SPECIFIC
    FIRST: (acct,src) > (acct,*) > (*,src) > (*,*). A NULL scope column matches
    anything; specificity = exact-account (2) + exact-source (1).

    Pillars CASCADE down this chain (#104): a strategy that leaves a pillar unset
    inherits it from the next-less-specific match — ultimately the (Any, Any) base
    row — instead of jumping to a code default. That is what makes (Any, Any) a
    real base layer and lets Strategies be the single source of truth, with no
    hidden global settings layer underneath."""
    scored = []
    for s in strategies or []:
        if not getattr(s, "enabled", True):
            continue
        sa, ss = getattr(s, "account_id", None), getattr(s, "source_id", None)
        if sa is not None and sa != account_id:
            continue
        if ss is not None and ss != source_id:
            continue
        scored.append(((2 if sa == account_id else 0) + (1 if ss == source_id else 0), s))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored]


def resolve_strategy(strategies, account_id, source_id):
    """The most-specific matching strategy — used for attribution (which arm a
    trade ran under) and the config preview. Pillar resolution uses the whole
    chain (resolve_chain), not just this row."""
    chain = resolve_chain(strategies, account_id, source_id)
    return chain[0] if chain else None


def _as_chain(x) -> list:
    """Accept a chain (list, most-specific first), a single strategy, or None."""
    if x is None:
        return []
    if isinstance(x, (list, tuple)):
        return [s for s in x if s is not None]
    return [x]


def _pillar(strategy, name) -> dict:
    return (getattr(strategy, name, None) or {}) if strategy is not None else {}


# ---- Exit pillar -------------------------------------------------------------
def exit_sl_rules(chain, *, source_rules=None, global_default=None) -> tuple:
    """Effective exit ladder + origin, cascading most-specific -> (Any,Any) ->
    source.strategy.sl_rules -> global default_sl_rules -> built-in. The list is
    copied so callers can snapshot it without aliasing stored config."""
    for s in _as_chain(chain):
        r = _pillar(s, "exit_policy").get("sl_rules")
        if r:
            return list(r), "strategy"
    if source_rules:
        return list(source_rules), "source"
    if global_default:
        return list(global_default), "global"
    return list(DEFAULT_SL_RULES), "default"


def cancel_pending_on_stop(chain, *, source_strategy=None, default=True) -> bool:
    """First strategy in the chain that defines it wins; else legacy source; else default."""
    for s in _as_chain(chain):
        ep = _pillar(s, "exit_policy")
        if "cancel_pending_on_stop" in ep:
            return bool(ep["cancel_pending_on_stop"])
    if source_strategy and "cancel_pending_on_stop" in source_strategy:
        return bool(source_strategy["cancel_pending_on_stop"])
    return default


# What `cancel_pending_on_stop` retires, and why.
CANCEL_PROGRESSED = "progressed"
CANCEL_STOPPED_OUT = "stopped_out"


def cancel_reason(leg_statuses, *, tps_hit: bool = False,
                  sl_moved: bool = False) -> "str | None":
    """Why a trade's still-resting entry orders should be retired now, or None to
    leave them working.

      `stopped_out` — a leg filled and NO position of the trade is open any more.
        The trade is over; anything still resting at the broker (a stale ladder
        rung, or a staged reclaim STOP that is ARMED) would open a brand-new,
        UNMANAGED position on a dead trade — no ratchet, no exit rules, no risk
        accounting — and today only the leg TTL ever retires it, up to an hour
        later (#161). `cancel_pending_on_stop` is named for exactly this case, but
        only ever implemented the next one.
      `progressed` — a leg filled AND the trade moved our way (a TP was reached
        or a stop ratcheted). The remaining unfilled rungs are stale: price went
        where we wanted without them, so filling now would be entering late (#25).

    Pass EVERY leg of the trade (any status), so a position that filled and closed
    on an earlier tick still counts as filled. Both reasons require a real fill —
    a phantom TP computed off price with zero fills must never tear down a ladder
    we were never in (#25).
    """
    statuses = list(leg_statuses)
    if not any(s in ("open", "closed") for s in statuses):
        return None
    if not any(s == "open" for s in statuses):
        return CANCEL_STOPPED_OUT              # strictly stronger than progressed
    if tps_hit or sl_moved:
        return CANCEL_PROGRESSED
    return None


# ---- Entry pillar ------------------------------------------------------------
def entry_policy(chain, *, global_planner=None, source_ttl=None) -> dict:
    """Merged entry policy (#104): built-in planner defaults, a legacy source TTL,
    then every matching strategy applied LEAST- to MOST-specific — so a specific
    row overrides only the keys it sets and inherits the rest from the (Any, Any)
    base. Keys in ENTRY_POLICY_KEYS."""
    merged = dict(global_planner or {})
    if source_ttl is not None:
        merged["ttl_minutes"] = source_ttl
    for s in reversed(_as_chain(chain)):            # least-specific first
        for k, v in _pillar(s, "entry_policy").items():
            if v is not None:
                merged[k] = v
    return merged


# ---- Filtration pillar -------------------------------------------------------
def resolve_entry_filters(chain, *, global_filters=None) -> dict:
    """Effective entry_filters for this scope (#104): the most-specific non-empty
    block wins wholesale (predictable — a channel's filter set is not half-merged),
    cascading down to the (Any, Any) base. `global_filters` is only a code-level
    floor; the legacy global `entry_filters` SETTING is no longer consulted by the
    executor — Strategies is the single source of truth."""
    for s in _as_chain(chain):
        ef = getattr(s, "entry_filters", None)
        if ef:
            return dict(ef)
    return dict(global_filters or {})


def _adx_block(ctx, timeframe):
    """The per-timeframe ADX block for an `adx_regime` rule (#127). ctx carries
    `adx` as {tf: {"adx": float, "trending": bool}} (built from the persisted
    per-TF `adx_14` feature). `timeframe` selects the TF; when omitted, the only
    entry is used (ambiguous multi-TF without an explicit TF stays a no-op).
    Returns the block dict, or None when the input isn't present (fail-open)."""
    adx = ctx.get("adx")
    if not isinstance(adx, dict) or not adx:
        return None
    if timeframe:
        b = adx.get(timeframe)
        return b if isinstance(b, dict) else None
    if len(adx) == 1:
        b = next(iter(adx.values()))
        return b if isinstance(b, dict) else None
    return None


def _as_num(v):
    """`float(v)`, or None when v is missing, blank or unparseable (#164).

    The SINGLE definition of "this numeric sub-condition was not supplied". The
    UI stores a cleared numeric field as `""` — `EntryFilterRules.jsx` ships
    `min_adx: ""` in the ADX Regime blank and labels it optional — and the API's
    `_clean_entry_filters` validates only `when.type` / `action`, so `""` reaches
    the evaluator straight from the normal save path.

    An `is not None` guard followed by a bare `float()` let that through and
    raised inside `handle_signal`, which the executor's consumer loop swallows
    AFTER the signal is off the durable queue: no trade, no `entry_filtered`
    event, one stack trace. Reading a bad bound as absent (fail open) is the
    only safe behaviour — a filtration rule must never be able to delete a
    signal by crashing."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _eval_adx_regime(when, ctx):
    """`adx_regime` condition (#127): match on the per-TF ADX trend state.

    Keys: timeframe (which TF's ADX), trending (bool — match when the TF's ADX
    `trending` equals this), min_adx / max_adx (numeric ADX bounds). Every
    supplied sub-condition must hold.

    Tri-state (#184): `None` means UNKNOWN — the referenced ADX value is absent
    (no `adx` in ctx, TF missing, the field is None) or the rule states no
    condition at all. Both used to be `False`, and for filtration they still
    read as False; the distinction exists so `not` cannot turn "we could not
    look" into "it did not hold" (see `evaluate_condition`)."""
    block = _adx_block(ctx, when.get("timeframe"))
    if block is None:
        return None
    want_trending = when.get("trending")
    min_adx, max_adx = _as_num(when.get("min_adx")), _as_num(when.get("max_adx"))
    if want_trending is None and min_adx is None and max_adx is None:
        return None                                   # no condition -> no-op, not match-all
    if want_trending is not None:
        tr = block.get("trending")
        if tr is None:
            return None                               # missing input, not a mismatch
        if bool(tr) != bool(want_trending):
            return False
    adx_val = _as_num(block.get("adx"))
    if min_adx is not None or max_adx is not None:
        if adx_val is None:
            return None
        if min_adx is not None and adx_val < min_adx:
            return False
        if max_adx is not None and adx_val > max_adx:
            return False
    return True


def _match_adx_regime(when, ctx) -> bool:
    """Boolean face of `_eval_adx_regime` — UNKNOWN reads as "does not match"."""
    return _eval_adx_regime(when, ctx) is True


def _eval_mc_probability(when, ctx):
    """`mc_probability` condition: match on the Monte Carlo GEOMETRY NULL for this
    signal (analysis/montecarlo.py) — what the entry/SL/TP layout is worth before
    any channel skill is assumed.

    Keys: min_p_win / max_p_win (bounds on `p_win_geometry`), min_expected_r /
    max_expected_r (bounds on `expected_r`), min_rr / max_rr (bounds on
    `rr_to_tp1`). Every supplied sub-condition must hold.

    The useful rule is `max_expected_r: 0` -> skip: a geometry whose expected R is
    negative under a no-skill null needs the channel to be genuinely good just to
    break even. A HIGH `p_win_geometry` is the opposite of a green light — it
    means the stop is far and the target near.

    FAIL-OPEN: absent ctx['montecarlo'] (or an absent field) is UNKNOWN, so the
    rule is inert until the executor plumbs the block in. A rule with no
    sub-condition stays a no-op rather than matching everything."""
    mc = ctx.get("montecarlo")
    if not isinstance(mc, dict) or not mc:
        return None
    bounds = (("min_p_win", "p_win_geometry", True), ("max_p_win", "p_win_geometry", False),
              ("min_expected_r", "expected_r", True), ("max_expected_r", "expected_r", False),
              ("min_rr", "rr_to_tp1", True), ("max_rr", "rr_to_tp1", False))
    # `_as_num` is the shared blank/unparseable guard (#164) — a cleared UI field
    # reads as absent here rather than raising into the executor's entry path.
    supplied = [(b, _as_num(when.get(b[0]))) for b in bounds]
    supplied = [(b, bound) for b, bound in supplied if bound is not None]
    if not supplied:
        return None                                   # no condition -> no-op
    for (_key, field, is_min), bound in supplied:
        val = _as_num(mc.get(field))
        if val is None:
            return None                               # missing input, not a mismatch
        if is_min and val < bound:
            return False
        if not is_min and val > bound:
            return False
    return True


def _match_mc_probability(when, ctx) -> bool:
    return _eval_mc_probability(when, ctx) is True


def _eval_turtle_signal(when, ctx):
    """`turtle_signal` condition: match on the Donchian breakout state at signal
    time (analysis/turtle.py) — a mechanical second opinion on the channel's call.

    Keys: agrees (bool — match when the Turtle side equals the signal direction),
    position (`long` / `short` / `flat`), variant (`signal` (reference,
    stop-and-reverse) or `signal_flat` (exits to flat); default `signal`).

    FAIL-OPEN: absent ctx['turtle'] or an absent field is UNKNOWN, so the rule is
    inert until the executor plumbs the block in."""
    tu = ctx.get("turtle")
    if not isinstance(tu, dict) or not tu:
        return None
    want_agrees, want_pos = when.get("agrees"), when.get("position")
    if want_agrees is None and not want_pos:
        return None                                   # no condition -> no-op
    if want_agrees is not None:
        got = tu.get("agrees")
        if got is None:
            return None
        if bool(got) != bool(want_agrees):
            return False
    if want_pos:
        key = "position_flat" if when.get("variant") == "signal_flat" else "position"
        got = tu.get(key)
        if not got or got == "unknown":
            return None                               # "we don't know" is not "no"
        if got != want_pos:
            return False
    return True


def _match_turtle_signal(when, ctx) -> bool:
    return _eval_turtle_signal(when, ctx) is True


# The shadow-analytics blocks a rule set needs in `ctx` before it can ever fire.
# Empty when no such rule is configured — so nothing is computed on the hot path
# by default. Both stay INERT until an A/B graduates them (the #127 -> #132 path).
_SHADOW_RULE_INPUTS = {"mc_probability": "montecarlo", "turtle_signal": "turtle"}


def shadow_rule_inputs(rules) -> set:
    """Which shadow blocks (`montecarlo` / `turtle`) this rule set references.
    Walks composed conditions to their leaves (#184)."""
    need = set()
    for r in rules or []:
        if not isinstance(r, dict):
            continue
        for leaf in condition_leaves(r.get("when") or {}):
            key = _SHADOW_RULE_INPUTS.get(leaf.get("type"))
            if key:
                need.add(key)
    return need


# ---- the generic indicator condition (#167) ---------------------------------
# One evaluator for the WHOLE TA registry: {"type":"indicator", "id", "timeframe",
# "field", "op", "value"|"ref", "params"}. Adding a gateable indicator is now
# adding an indicator — there is no matching evaluator to hand-write, and no ctx
# key to hand-plumb in the executor (that two-step is what #132 had to do for ADX
# alone, and it does not scale to a 45-entry registry).
DEFAULT_RULE_TIMEFRAME = "4h"

_NUM_OPS = {
    "gt": lambda a, b: a > b, "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b,
}
FILTER_OPS = frozenset({*_NUM_OPS, "eq", "ne", "between", "outside",
                        "is_true", "is_false"})

# Boolean composition over the leaf conditions (#184). Checked in this order, so
# a dict carrying both a composite key and a `type` reads as the composite.
COMPOSITE_KEYS = ("all", "any", "not")


def _as_bool(v):
    """True/False for a boolean-ish input, or None when it isn't one. Same posture
    as `_as_num`: unparseable reads as absent, never as False."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1"):
            return True
        if s in ("false", "no", "0"):
            return False
    return None


def _loose_eq(left, right) -> bool:
    """Equality across the three output flavours the registry emits: booleans
    (`trending`, `above`), numbers, and short strings (`cross`, `trend`)."""
    if isinstance(left, bool):
        rb = _as_bool(right)
        return rb is not None and left is rb
    ln, rn = _as_num(left), _as_num(right)
    if ln is not None and rn is not None:
        return ln == rn
    return str(left).strip().lower() == str(right).strip().lower()


def _ta_block(ctx, indicator_id, timeframe, key=None):
    """The outputs dict for one indicator on one timeframe, or None (fail-open).

    Looks up the exact instance key the executor wrote (`rsi_14`), then the bare
    id, then a unique `<id>_…` instance — so a hand-built ctx in a test or a
    future caller that keys by id still resolves, while an ambiguous match (two
    EMAs on the same TF) deliberately resolves to nothing rather than to a coin
    flip."""
    ta = ctx.get("ta")
    if not isinstance(ta, dict) or not ta or not indicator_id:
        return None
    if timeframe:
        block = ta.get(timeframe)
    elif len(ta) == 1:
        block = next(iter(ta.values()))
    else:
        block = None                    # ambiguous multi-TF without a TF -> no-op
    if not isinstance(block, dict):
        return None
    for k in ([key] if key else []) + [indicator_id]:
        v = block.get(k)
        if isinstance(v, dict):
            return v
    prefix = f"{indicator_id}_"
    hits = [v for k, v in block.items()
            if isinstance(v, dict) and k.startswith(prefix)]
    return hits[0] if len(hits) == 1 else None


def _ta_field(ctx, when, key=None):
    block = _ta_block(ctx, when.get("id"), when.get("timeframe"), key)
    if block is None:
        return None
    return block.get(when.get("field") or "value")


def indicator_value(ctx, ref):
    """One indicator field out of `ctx['ta']`, or None (#184).

    Public because a `generator:rules` config resolves entry / SL / TP LEVELS
    from the same ctx its conditions read — `{"type": "level", "id":
    "order_block", "timeframe": "15m", "field": "bottom"}` is the far edge of the
    block the condition just matched on. Same lookup, same fail-open: a level
    that cannot be read is None, and the caller drops the signal rather than
    inventing a price."""
    if not isinstance(ref, dict):
        return None
    return _ta_field(ctx, ref, _rule_instance_key(ref))


def _right_operand(when, ctx):
    """The value the left side is compared against: `ref` (another indicator field,
    or the live price) when present, else the literal `value`. `ref` is what makes
    price-vs-band and band-vs-band gates expressible without a bespoke rule type."""
    ref = when.get("ref")
    if ref not in (None, ""):
        if isinstance(ref, str):
            return ctx.get("price") if ref.strip().lower() == "price" else None
        if isinstance(ref, dict):
            return _ta_field(ctx, {"id": ref.get("id"),
                                   "timeframe": ref.get("timeframe") or when.get("timeframe"),
                                   "field": ref.get("field")},
                             _rule_instance_key(ref))
        return None
    v = when.get("value")
    return None if v == "" else v


def _rule_instance_key(when):
    """The `<id>_<params>` key this rule's indicator lands under, or None if the id
    isn't in the registry."""
    inst = TA.resolve_instance((when or {}).get("id"), (when or {}).get("params"))
    return inst["key"] if inst else None


def _eval_indicator(when, ctx):
    """`indicator` condition (#167): compare one field of one registry indicator on
    one timeframe against a literal or another field.

    FAIL-OPEN throughout, exactly as `_eval_adx_regime`: an unknown op, an
    indicator that isn't in ctx['ta'], a missing field, an unparseable bound — all
    of them are UNKNOWN, which reads as "does not match". A filtration rule must
    never be able to delete a signal by being wrong about its own inputs, and a
    generator must never EMIT one on the absence of evidence (#184)."""
    op = str(when.get("op") or "").strip().lower()
    if op not in FILTER_OPS:
        return None
    left = _ta_field(ctx, when, _rule_instance_key(when))
    if left is None:
        return None
    if op in ("is_true", "is_false"):
        lb = _as_bool(left)
        return None if lb is None else (lb is (op == "is_true"))
    if op in ("between", "outside"):
        pair = when.get("value")
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return None
        lo, hi, val = _as_num(pair[0]), _as_num(pair[1]), _as_num(left)
        if lo is None or hi is None or val is None:
            return None
        if lo > hi:
            lo, hi = hi, lo
        inside = lo <= val <= hi
        return inside if op == "between" else not inside
    right = _right_operand(when, ctx)
    if right is None:
        return None
    if op in ("eq", "ne"):
        return _loose_eq(left, right) is (op == "eq")
    lv, rv = _as_num(left), _as_num(right)
    if lv is None or rv is None:
        return None
    return _NUM_OPS[op](lv, rv)


def _match_indicator(when, ctx) -> bool:
    return _eval_indicator(when, ctx) is True


def _requirements_of(when, default_timeframe) -> list:
    """The (indicator, timeframe) instances one `indicator` rule needs computed —
    its own, plus its `ref` side when that references another indicator."""
    out = []
    for side in (when, when.get("ref") if isinstance(when.get("ref"), dict) else None):
        if not side or not side.get("id"):
            continue
        inst = TA.resolve_instance(side["id"], side.get("params"))
        if not inst:
            continue                                   # unknown id -> inert rule
        tf = side.get("timeframe") or when.get("timeframe") or default_timeframe
        if tf not in TA.AVAILABLE_TIMEFRAMES:
            continue
        out.append({**inst, "timeframe": tf})
    return out


def condition_requirements(cond, default_timeframe: str = DEFAULT_RULE_TIMEFRAME) -> list:
    """Every indicator instance ONE condition tree needs computed, deduped.

    The generator's entry point (#184): a `generator:rules` config asks its own
    condition what to compute and the harness resamples exactly that, once per
    timeframe for the whole run. Same walk as `ta_rule_requirements`, minus the
    rule-list wrapper."""
    seen, out = set(), []
    for leaf in condition_leaves(cond):
        if leaf.get("type") != "indicator":
            continue
        for req in _requirements_of(leaf, default_timeframe):
            token = (req["timeframe"], req["key"])
            if token not in seen:
                seen.add(token)
                out.append(req)
    return out


def ta_rule_requirements(rules, default_timeframe: str = DEFAULT_RULE_TIMEFRAME) -> list:
    """Every indicator instance an `indicator` rule set needs, deduped, as
    [{id, params, key, outputs, timeframe}].

    This is the generalisation of `adx_rule_timeframes`: the executor asks the rule
    set what it needs, fetches each referenced timeframe's bars ONCE, and computes
    only those registry entries. Empty when no rule references TA — so the default
    install still fetches nothing extra on the entry path.

    SHADOW rules are included: a rule that isn't allowed to act still has to be
    computed, or there is nothing to measure and no way to ever promote it.

    Composed conditions are walked to their leaves (#184) — an `indicator` buried
    in an `all`/`any`/`not` needs computing exactly as a flat one does, and one
    that wasn't would be a rule that silently never fires."""
    seen, out = set(), []
    for r in rules or []:
        if not isinstance(r, dict) or not r.get("enabled", True):
            continue
        for req in condition_requirements(r.get("when") or {}, default_timeframe):
            token = (req["timeframe"], req["key"])
            if token not in seen:
                seen.add(token)
                out.append(req)
    return out


# ---- shadow vs live (#167 guardrail) ----------------------------------------
# Generic gating over a 45-entry registry is a false-discovery machine: at N≈50–100
# correlated XAUUSD trades an unconstrained "gate on anything" API manufactures
# significant-looking rules every week by chance. So a rule carries a `mode`, and
# only `live` may skip or scale.
FILTER_MODES = ("live", "shadow")
# The generic `indicator` condition defaults to SHADOW — a newly authored rule must
# not be able to change live behaviour by existing. The condition types that
# predate it default to LIVE, because they are already deployed and configured and
# flipping them to shadow would silently UN-gate trades that are being filtered
# today (#127/#132's adx_regime is live on purpose). An explicit `mode` always wins.
_SHADOW_DEFAULT_TYPES = frozenset({"indicator"})


def rule_mode(rule) -> str:
    """`live` or `shadow` for one rule. See `_SHADOW_DEFAULT_TYPES` for why the
    default depends on the condition type.

    A COMPOSED condition (#184) is shadow-by-default if ANY leaf is: wrapping an
    `indicator` in an `all` must not be a way to promote it to live by accident,
    and the composite dict has no `type` of its own to read."""
    if not isinstance(rule, dict):
        return "live"
    m = str((rule.get("mode") or "")).strip().lower()
    if m in FILTER_MODES:
        return m
    types = {leaf.get("type") for leaf in condition_leaves(rule.get("when") or {})}
    return "shadow" if types & _SHADOW_DEFAULT_TYPES else "live"


def filter_mode_counts(rules) -> dict:
    """{'live': n, 'shadow': n} over the ENABLED rules — the count the API and UI
    surface so the number of simultaneous live gates (each one another
    multiple-comparison) is visible rather than buried in a JSON blob."""
    counts = {"live": 0, "shadow": 0}
    for r in rules or []:
        if isinstance(r, dict) and r.get("enabled", True):
            counts[rule_mode(r)] += 1
    return counts


class FilterDecision(NamedTuple):
    """What the filtration pillar decided, plus what the shadow rules WOULD have
    decided. `factor`/`skip`/`reasons` reflect live rules only.

    `evaluated` is the audit trail (#213): one record per ENABLED rule, live or
    shadow, carrying the leaf values that decided it. `reasons` stays a list of
    NAMES so every existing consumer of the skip event is unaffected."""
    factor: float
    skip: bool
    reasons: list
    shadow: list
    evaluated: list = []


def adx_rule_timeframes(rules, default: str = "4h") -> set:
    """The timeframes an `adx_regime` rule set needs live ADX computed for (#132).
    A rule without an explicit `timeframe` falls back to `default`. Empty when no
    adx_regime rule is present — so the executor computes (and fetches) nothing
    unless a rule actually references ADX (keeps the hot path free by default).
    Walks composed conditions to their leaves (#184)."""
    tfs = set()
    for r in rules or []:
        if not isinstance(r, dict):
            continue
        for leaf in condition_leaves(r.get("when") or {}):
            if leaf.get("type") == "adx_regime":
                tfs.add(leaf.get("timeframe") or default)
    return tfs


# ---- time_window -------------------------------------------------------------
# Hour-of-day is the strongest entry-side effect visible in the live book, and
# `session_in` cannot express it: a session is a NAME, and by session everything
# is negative with a weak spread, while by hour the structure is sharp (#214).
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _as_minute_of_day(v):
    """"HH:MM" (or "HH:MM:SS") -> minutes since midnight, else None. `24:00` is
    accepted as end-of-day so a window can close on the boundary."""
    if v is None:
        return None
    txt = str(v).strip()
    if not txt:
        return None
    parts = txt.split(":")
    if len(parts) < 2 or len(parts) > 3:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return None
    if h == 24 and m == 0:
        return 24 * 60
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    return h * 60 + m


def _as_datetime(v):
    """A ctx timestamp as an AWARE datetime, else None. Accepts a datetime (a
    naive one is read as UTC, which is what every producer here stores) or an
    ISO-8601 string, including the `Z` suffix Postgres/JSON round-trips."""
    if isinstance(v, _dt.datetime):
        return v if v.tzinfo is not None else v.replace(tzinfo=_dt.timezone.utc)
    if isinstance(v, str):
        txt = v.strip()
        if not txt:
            return None
        if txt.endswith(("Z", "z")):
            txt = txt[:-1] + "+00:00"
        try:
            d = _dt.datetime.fromisoformat(txt)
        except ValueError:
            return None
        return d if d.tzinfo is not None else d.replace(tzinfo=_dt.timezone.utc)
    return None


def _in_zone(when, moment):
    """`moment` moved into the leaf's `tz`, or None when that zone is unknown.
    Unknown zone -> UNKNOWN, never a silent UTC reading: a window an operator
    wrote in New York time must not fire on London hours."""
    tz = str(when.get("tz") or "UTC").strip()
    if tz.upper() in ("UTC", "Z", "GMT"):
        return moment.astimezone(_dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return moment.astimezone(ZoneInfo(tz))
    except Exception:
        return None


def _eval_time_window(when, ctx):
    """`{type: time_window, tz, from, to, days}` — does the signal's own instant
    fall inside a wall-clock window? Tri-state like every other leaf.

    `[from, to)`, half-open, so back-to-back windows do not both claim the
    boundary minute. `to <= from` CROSSES MIDNIGHT (22:00->06:00 is the evening
    plus the early morning), which is the only reading that makes an overnight
    window expressible at all. `days` (mon..sun) additionally restricts the
    weekday OF THE TIMESTAMP itself, in the window's own zone.

    UNKNOWN when: no timestamp in ctx, an unparseable/absent bound, an unknown
    timezone, or an unparseable day name. Never False on a missing input — a
    `{"not": ...}` around this leaf must not fire because we could not read the
    clock (#184)."""
    moment = _as_datetime(ctx.get("ts"))
    if moment is None:
        return None
    lo = _as_minute_of_day(when.get("from"))
    hi = _as_minute_of_day(when.get("to"))
    if lo is None or hi is None or lo == hi:
        return None                               # no window stated -> no-op
    local = _in_zone(when, moment)
    if local is None:
        return None
    days = when.get("days")
    if days:
        try:
            want = {_WEEKDAYS[str(d).strip().lower()[:3]] for d in days}
        except KeyError:
            return None                           # unreadable day -> unknown
        if local.weekday() not in want:
            return False
    minute = local.hour * 60 + local.minute
    if lo < hi:
        return lo <= minute < hi
    return minute >= lo or minute < hi            # crosses midnight


# ---- what a rule actually READ ----------------------------------------------
# #167 made arming a gate a config act; capture and the skip event did not
# follow. The result was a rule doing the most work in the whole experiment
# (`bt_1h_cci_value_gte100`: 51 removals, -19,430 AED) about which the database
# could not answer what value fired it, what it would have done elsewhere, or
# whether it still separates. A rule that cannot be observed cannot be reviewed.
def leaf_reading(leaf, ctx):
    """The INPUT one leaf actually read out of `ctx`, or None when it read
    nothing. Never raises and never evaluates side-effects — this is the audit
    trail, so it must not be able to change what the gate decided."""
    if not isinstance(leaf, dict):
        return None
    t = leaf.get("type")
    if t == "indicator":
        return _ta_field(ctx, leaf, _rule_instance_key(leaf))
    if t == "adx_regime":
        # The evaluator's own lookup, single-entry fallback included: an audit
        # trail that reads a different place from the gate is worse than none.
        return _adx_block(ctx, leaf.get("timeframe"))
    if t == "session_in":
        have = ctx.get("sessions")
        if have is None and ctx.get("session") is not None:
            have = [ctx["session"]]
        return have
    if t == "time_window":
        ts = ctx.get("ts")
        return ts if ts is None or isinstance(ts, str) else str(ts)
    if t == "mc_probability":
        return ctx.get("montecarlo")
    if t == "turtle_signal":
        return ctx.get("turtle")
    return None


def explain_condition(cond, ctx) -> list:
    """One record per LEAF of a condition: what it asked, what it read, and how
    it resolved. JSON-safe, so it can ride on the event payload.

    `result` is the leaf's own tri-state, NOT the rule's: a composed rule that
    skipped is explained by which of its leaves held and which could not be
    computed at all. `actual: null` with `result: null` is the signature of a
    gate firing on an input nobody is capturing."""
    out = []
    for leaf in condition_leaves(cond):
        if not isinstance(leaf, dict):
            continue
        rec = {"type": leaf.get("type")}
        for k in ("id", "timeframe", "field", "op", "value", "trending",
                  "min_adx", "max_adx", "sessions", "from", "to", "tz", "days"):
            if k in leaf and leaf[k] not in (None, ""):
                rec[k] = leaf[k]
        actual = leaf_reading(leaf, ctx)
        rec["actual"] = actual if _json_safe(actual) else str(actual)
        rec["result"] = _eval_leaf(leaf, ctx)
        out.append(rec)
    return out


def _json_safe(v) -> bool:
    if v is None or isinstance(v, (bool, int, float, str)):
        return True
    if isinstance(v, (list, tuple)):
        return all(_json_safe(x) for x in v)
    if isinstance(v, dict):
        return all(isinstance(k, str) and _json_safe(x) for k, x in v.items())
    return False


def _eval_leaf(when, ctx):
    """One LEAF condition against the context, tri-state.

    `True` it held · `False` it did not · `None` we could not tell (the input is
    not in ctx, the rule states no condition, or a bound is unparseable). Every
    branch is fail-open: an unrecognised type is UNKNOWN, never True."""
    if not isinstance(when, dict):
        return None
    wtype = when.get("type")
    if wtype == "always":
        return True
    if wtype == "session_in":
        have = ctx.get("sessions")
        if have is None and ctx.get("session") is not None:
            have = [ctx["session"]]
        if not have:
            return None                               # no session info -> unknown
        want = when.get("sessions") or []
        if not want:
            return None                               # no condition -> no-op
        return any(s in want for s in have)
    if wtype == "time_window":
        return _eval_time_window(when, ctx)
    if wtype == "adx_regime":
        return _eval_adx_regime(when, ctx)
    if wtype == "mc_probability":
        return _eval_mc_probability(when, ctx)
    if wtype == "turtle_signal":
        return _eval_turtle_signal(when, ctx)
    if wtype == "indicator":
        return _eval_indicator(when, ctx)
    # (structure/bayesian conditions plug in here — no-op until wired)
    return None


def evaluate_condition(cond, ctx):
    """A whole condition TREE against the context, tri-state (#184).

    ONE GRAMMAR, TWO USES. `entry_filters` asks "should I filter this signal?";
    a generator asks the same expression, bar by bar, "should I EMIT one?".
    Adding a leaf type therefore benefits filtration and generation at once —
    which is the entire reason this is shared rather than reimplemented, and the
    reason #167's mistake (one hand-written evaluator per idea) is not repeated
    on the generation side.

        {"all": [c, ...]}  every sub must hold
        {"any": [c, ...]}  at least one must hold
        {"not": c}         the inverse
        {"type": ...}      a leaf (see `_eval_leaf`)

    THREE-VALUED, and that is the point of the whole design. Kleene logic:
    `not UNKNOWN` is UNKNOWN, `all` is UNKNOWN unless something is definitely
    False, `any` is UNKNOWN unless something is definitely True. Two-valued
    logic with `False` standing in for "missing" would make `{"not": X}` fire
    whenever X's inputs were absent — a generator trading on the absence of
    evidence, which is exactly the failure fail-open exists to prevent. An empty
    `all`/`any` is UNKNOWN too, not vacuous truth: an unfinished rule must not
    match everything.

    Callers that want a decision use `matches_condition` — UNKNOWN reads as "did
    not match", so filtration behaviour is byte-identical to before this split."""
    if not isinstance(cond, dict):
        return None
    for key in COMPOSITE_KEYS:
        if key not in cond:
            continue
        if key == "not":
            inner = evaluate_condition(cond[key], ctx)
            return None if inner is None else (not inner)
        subs = cond[key]
        if not isinstance(subs, (list, tuple)) or not subs:
            return None                               # empty -> no-op, not match-all
        vals = [evaluate_condition(c, ctx) for c in subs]
        if key == "all":
            if any(v is False for v in vals):
                return False
            return None if any(v is None for v in vals) else True
        if any(v is True for v in vals):
            return True
        return None if any(v is None for v in vals) else False
    return _eval_leaf(cond, ctx)


def matches_condition(cond, ctx) -> bool:
    """Boolean face of `evaluate_condition`: UNKNOWN means "does not match"."""
    return evaluate_condition(cond, ctx) is True


def condition_leaves(cond):
    """Every LEAF in a (possibly composed) condition tree, in authored order.

    What `ta_rule_requirements`, `adx_rule_timeframes` and `shadow_rule_inputs`
    walk, so a nested rule declares its inputs exactly as a flat one does — a
    composite whose indicators were never computed would be silently inert."""
    if not isinstance(cond, dict):
        return
    for key in COMPOSITE_KEYS:
        if key not in cond:
            continue
        subs = [cond[key]] if key == "not" else cond[key]
        if isinstance(subs, (list, tuple)):
            for s in subs:
                yield from condition_leaves(s)
        return
    yield cond


def _matches(when, ctx) -> bool:
    """Does one rule's condition hold against the trade context? Fail-open."""
    return matches_condition(when, ctx)


def _scale_factor(rule) -> float:
    try:
        return max(0.0, float(rule.get("factor", 1.0)))
    except (TypeError, ValueError):
        return 1.0


def evaluate_filter_rules(rules, ctx) -> FilterDecision:
    """Evaluate the extensible filtration rules against a trade CONTEXT (#84).

    Each rule: {enabled, name, mode:'live'|'shadow', when:{type, ...},
    action:'skip'|'scale', factor}. Rules compose multiplicatively for 'scale';
    any matched 'skip' wins. A rule whose condition inputs are missing from ctx is
    a no-op (fail-open) — so rules needing entry-time features simply don't fire
    until those features are wired. Understood conditions:
      session_in {sessions:[...]}          ctx['session'] in list
      time_window {tz, from, to, days}     ctx['ts'] inside a wall-clock window
                                             (#214); half-open, crosses midnight
      always                               unconditional (baseline scaling)
      adx_regime {timeframe, trending,     ctx['adx'][tf] ADX trend state (#127);
                  min_adx, max_adx}          fail-open when ADX absent from ctx
      mc_probability {min_p_win, max_p_win, ctx['montecarlo'] geometry null —
                      min/max_expected_r,     what the SL/TP layout is worth with
                      min_rr, max_rr}         NO channel skill. Inert until wired.
      turtle_signal {agrees, position,      ctx['turtle'] Donchian breakout state
                     variant}                 at signal time. Inert until wired.
      indicator {id, timeframe, field,     ctx['ta'][tf][key] — ANY registry
                 op, value|ref, params}      indicator (#167). SHADOW by default.

    A `when` may also COMPOSE those leaves (#184): {"all": [...]}, {"any": [...]},
    {"not": {...}} nest to any depth and are evaluated by `evaluate_condition` —
    the same expression a `generator:rules` backtest uses to decide whether to
    EMIT a signal. Composition is three-valued so `not` over a missing input
    stays inert; a composed rule containing an `indicator` leaf is shadow by
    default, exactly as the flat form is.

    A `shadow` rule is evaluated and RECORDED but never applied: it contributes
    nothing to factor/skip/reasons, and lands in `shadow` as {name, type, mode,
    matched, action, factor} so the executor can emit `filter_shadow` and the
    would-have-happened can be measured against outcomes. Promotion to `live` is a
    deliberate config act that has to clear the documented bar (CLAUDE.md §2.4)."""
    factor, skip, reasons, shadow, evaluated = 1.0, False, [], [], []
    for r in rules or []:
        if not isinstance(r, dict) or not r.get("enabled", True):
            continue
        when = r.get("when") or {}
        wtype = when.get("type")
        matched = _matches(when, ctx)
        mode = rule_mode(r)
        # Recorded for BOTH modes and whether it matched or not: a rule has to be
        # measurable BEFORE it is armed, and a live gate's non-matches are half
        # the evidence for ever keeping it (#213).
        # `matched` here is TRI-STATE, unlike the boolean the decision uses:
        # "could not tell" and "did not hold" are the same non-skip but not the
        # same evidence, and telling them apart is the whole point (#213).
        evaluated.append({"name": r.get("name") or wtype or "rule",
                          "type": wtype, "mode": mode, "action": r.get("action"),
                          "matched": evaluate_condition(when, ctx),
                          "leaves": explain_condition(when, ctx)})
        if mode == "shadow":
            # Recorded whether it matched or not — a gate's non-matches are half
            # of the evidence you need to ever justify promoting it.
            shadow.append({"name": r.get("name") or wtype or "rule", "type": wtype,
                           "mode": mode, "matched": bool(matched),
                           "action": r.get("action"),
                           "factor": _scale_factor(r) if r.get("action") == "scale" else None})
            continue
        if not matched:
            continue
        if r.get("action") == "skip":
            skip = True
            reasons.append(r.get("name") or wtype or "skip")
        elif r.get("action") == "scale":
            factor *= _scale_factor(r)
            reasons.append(r.get("name") or wtype or "scale")
    return FilterDecision(factor, skip, reasons, shadow, evaluated)


def apply_filter_rules(rules, ctx) -> tuple:
    """(size_factor, skip, reasons) — the live decision only. Kept as the narrow
    entry point for callers that don't care about the shadow record."""
    d = evaluate_filter_rules(rules, ctx)
    return d.factor, d.skip, d.reasons
