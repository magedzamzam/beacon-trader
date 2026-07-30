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

from ..strategy.rules import DEFAULT_SL_RULES

# The entry-policy keys the planner/executor understand (chase guard #67 + TTL).
# entry_style + staged drive the confirmation-staged entry model (#129); staged is
# a nested block validated by execution.staging.clean_staged_config.
ENTRY_POLICY_KEYS = ("ttl_minutes", "honor_market_hint", "chase_tolerance_r",
                     "chase_tolerance_atr", "beyond_tolerance", "max_tp_distance_pct",
                     "entry_style", "staged")


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


def _match_adx_regime(when, ctx) -> bool:
    """`adx_regime` condition (#127): match on the per-TF ADX trend state.

    Keys: timeframe (which TF's ADX), trending (bool — match when the TF's ADX
    `trending` equals this), min_adx / max_adx (numeric ADX bounds). Every
    supplied sub-condition must hold. FAIL-OPEN: if the referenced ADX value is
    absent (no `adx` in ctx, TF missing, or the specific field is None), the rule
    does NOT match — so it's a no-op until the ADX is plumbed into ctx (the
    measure-before-gate posture: the evaluator ships inert). A rule with no
    sub-condition also stays a no-op rather than matching everything."""
    block = _adx_block(ctx, when.get("timeframe"))
    if block is None:
        return False
    want_trending = when.get("trending")
    min_adx, max_adx = _as_num(when.get("min_adx")), _as_num(when.get("max_adx"))
    if want_trending is None and min_adx is None and max_adx is None:
        return False                                  # no condition -> no-op, not match-all
    if want_trending is not None:
        tr = block.get("trending")
        if tr is None or bool(tr) != bool(want_trending):
            return False
    adx_val = _as_num(block.get("adx"))
    if min_adx is not None and (adx_val is None or adx_val < min_adx):
        return False
    if max_adx is not None and (adx_val is None or adx_val > max_adx):
        return False
    return True


def _match_mc_probability(when, ctx) -> bool:
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

    FAIL-OPEN: absent ctx['montecarlo'] (or an absent field) does NOT match, so
    the rule is inert until the executor plumbs the block in. A rule with no
    sub-condition stays a no-op rather than matching everything."""
    mc = ctx.get("montecarlo")
    if not isinstance(mc, dict) or not mc:
        return False
    bounds = (("min_p_win", "p_win_geometry", True), ("max_p_win", "p_win_geometry", False),
              ("min_expected_r", "expected_r", True), ("max_expected_r", "expected_r", False),
              ("min_rr", "rr_to_tp1", True), ("max_rr", "rr_to_tp1", False))
    # `_as_num` is the shared blank/unparseable guard (#164) — a cleared UI field
    # reads as absent here rather than raising into the executor's entry path.
    supplied = [(b, _as_num(when.get(b[0]))) for b in bounds]
    supplied = [(b, bound) for b, bound in supplied if bound is not None]
    if not supplied:
        return False                                  # no condition -> no-op
    for (_key, field, is_min), bound in supplied:
        val = _as_num(mc.get(field))
        if val is None:
            return False                              # fail-open on a missing input
        if is_min and val < bound:
            return False
        if not is_min and val > bound:
            return False
    return True


def _match_turtle_signal(when, ctx) -> bool:
    """`turtle_signal` condition: match on the Donchian breakout state at signal
    time (analysis/turtle.py) — a mechanical second opinion on the channel's call.

    Keys: agrees (bool — match when the Turtle side equals the signal direction),
    position (`long` / `short` / `flat`), variant (`signal` (reference,
    stop-and-reverse) or `signal_flat` (exits to flat); default `signal`).

    FAIL-OPEN: absent ctx['turtle'] or an absent field does NOT match, so the rule
    is inert until the executor plumbs the block in."""
    tu = ctx.get("turtle")
    if not isinstance(tu, dict) or not tu:
        return False
    want_agrees, want_pos = when.get("agrees"), when.get("position")
    if want_agrees is None and not want_pos:
        return False                                  # no condition -> no-op
    if want_agrees is not None:
        got = tu.get("agrees")
        if got is None or bool(got) != bool(want_agrees):
            return False
    if want_pos:
        key = "position_flat" if when.get("variant") == "signal_flat" else "position"
        got = tu.get(key)
        if not got or got == "unknown" or got != want_pos:
            return False
    return True


# The shadow-analytics blocks a rule set needs in `ctx` before it can ever fire.
# Empty when no such rule is configured — so nothing is computed on the hot path
# by default. Both stay INERT until an A/B graduates them (the #127 -> #132 path).
_SHADOW_RULE_INPUTS = {"mc_probability": "montecarlo", "turtle_signal": "turtle"}


def shadow_rule_inputs(rules) -> set:
    """Which shadow blocks (`montecarlo` / `turtle`) this rule set references."""
    need = set()
    for r in rules or []:
        if not isinstance(r, dict):
            continue
        key = _SHADOW_RULE_INPUTS.get((r.get("when") or {}).get("type"))
        if key:
            need.add(key)
    return need


def adx_rule_timeframes(rules, default: str = "4h") -> set:
    """The timeframes an `adx_regime` rule set needs live ADX computed for (#132).
    A rule without an explicit `timeframe` falls back to `default`. Empty when no
    adx_regime rule is present — so the executor computes (and fetches) nothing
    unless a rule actually references ADX (keeps the hot path free by default)."""
    tfs = set()
    for r in rules or []:
        if isinstance(r, dict) and (r.get("when") or {}).get("type") == "adx_regime":
            tfs.add((r.get("when") or {}).get("timeframe") or default)
    return tfs


def apply_filter_rules(rules, ctx) -> tuple:
    """Evaluate the extensible filtration rules against a trade CONTEXT (#84).

    Each rule: {enabled, name, when:{type, ...}, action:'skip'|'scale', factor}.
    Returns (size_factor, skip, reasons). Rules compose multiplicatively for
    'scale'; any matched 'skip' wins. A rule whose condition inputs are missing
    from ctx is a no-op (fail-open) — so rules needing entry-time features simply
    don't fire until those features are wired. Currently understood conditions:
      session_in {sessions:[...]}          ctx['session'] in list
      always                               unconditional (baseline scaling)
      adx_regime {timeframe, trending,     ctx['adx'][tf] ADX trend state (#127);
                  min_adx, max_adx}          fail-open when ADX absent from ctx
      mc_probability {min_p_win, max_p_win, ctx['montecarlo'] geometry null —
                      min/max_expected_r,     what the SL/TP layout is worth with
                      min_rr, max_rr}         NO channel skill. Inert until wired.
      turtle_signal {agrees, position,      ctx['turtle'] Donchian breakout state
                     variant}                 at signal time. Inert until wired.
    Structure/bayesian conditions are declared here as they're added."""
    factor, skip, reasons = 1.0, False, []
    for r in rules or []:
        if not isinstance(r, dict) or not r.get("enabled", True):
            continue
        when = r.get("when") or {}
        wtype = when.get("type")
        matched = None
        if wtype == "always":
            matched = True
        elif wtype == "session_in":
            want = when.get("sessions") or []
            have = ctx.get("sessions")
            if have is None and ctx.get("session") is not None:
                have = [ctx["session"]]
            matched = bool(have) and any(s in want for s in have)
        elif wtype == "adx_regime":
            matched = _match_adx_regime(when, ctx)
        elif wtype == "mc_probability":
            matched = _match_mc_probability(when, ctx)
        elif wtype == "turtle_signal":
            matched = _match_turtle_signal(when, ctx)
        # (structure/bayesian conditions plug in here — no-op until wired)
        if not matched:
            continue
        if r.get("action") == "skip":
            skip = True
            reasons.append(r.get("name") or wtype or "skip")
        elif r.get("action") == "scale":
            try:
                factor *= max(0.0, float(r.get("factor", 1.0)))
            except (TypeError, ValueError):
                pass
            reasons.append(r.get("name") or wtype or "scale")
    return factor, skip, reasons
