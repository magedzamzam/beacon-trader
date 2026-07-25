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
    min_adx, max_adx = when.get("min_adx"), when.get("max_adx")
    if want_trending is None and min_adx is None and max_adx is None:
        return False                                  # no condition -> no-op, not match-all
    if want_trending is not None:
        tr = block.get("trending")
        if tr is None or bool(tr) != bool(want_trending):
            return False
    adx_val = block.get("adx")
    if min_adx is not None and (adx_val is None or float(adx_val) < float(min_adx)):
        return False
    if max_adx is not None and (adx_val is None or float(adx_val) > float(max_adx)):
        return False
    return True


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
