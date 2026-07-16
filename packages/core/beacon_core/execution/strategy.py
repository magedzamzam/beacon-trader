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
ENTRY_POLICY_KEYS = ("ttl_minutes", "honor_market_hint", "chase_tolerance_r",
                     "chase_tolerance_atr", "beyond_tolerance", "max_tp_distance_pct")


def resolve_strategy(strategies, account_id, source_id):
    """The most-specific ENABLED strategy for (account, source), or None.

    A NULL scope column matches anything; specificity = exact-account (2) +
    exact-source (1), so (acct,src) > (acct,*) > (*,src) > (*,*). Ties can't occur
    (each scope pair is unique)."""
    best, best_score = None, -1
    for s in strategies or []:
        if not getattr(s, "enabled", True):
            continue
        sa, ss = getattr(s, "account_id", None), getattr(s, "source_id", None)
        if sa is not None and sa != account_id:
            continue
        if ss is not None and ss != source_id:
            continue
        score = (2 if sa == account_id else 0) + (1 if ss == source_id else 0)
        if score > best_score:
            best, best_score = s, score
    return best


def _pillar(strategy, name) -> dict:
    return (getattr(strategy, name, None) or {}) if strategy is not None else {}


# ---- Exit pillar -------------------------------------------------------------
def exit_sl_rules(strategy, *, source_rules=None, global_default=None) -> tuple:
    """Effective exit ladder + origin. strategy.exit_policy.sl_rules ->
    source.strategy.sl_rules -> global default_sl_rules -> built-in. The list is
    copied so callers can snapshot it without aliasing stored config."""
    r = _pillar(strategy, "exit_policy").get("sl_rules")
    if r:
        return list(r), "strategy"
    if source_rules:
        return list(source_rules), "source"
    if global_default:
        return list(global_default), "global"
    return list(DEFAULT_SL_RULES), "default"


def cancel_pending_on_stop(strategy, *, source_strategy=None, default=True) -> bool:
    ep = _pillar(strategy, "exit_policy")
    if "cancel_pending_on_stop" in ep:
        return bool(ep["cancel_pending_on_stop"])
    if source_strategy and "cancel_pending_on_stop" in source_strategy:
        return bool(source_strategy["cancel_pending_on_stop"])
    return default


# ---- Entry pillar ------------------------------------------------------------
def entry_policy(strategy, *, global_planner=None, source_ttl=None) -> dict:
    """Merged entry policy: global planner defaults, then a legacy source TTL, then
    the strategy's entry_policy (only non-null keys win). Keys in ENTRY_POLICY_KEYS."""
    merged = dict(global_planner or {})
    if source_ttl is not None:
        merged["ttl_minutes"] = source_ttl
    for k, v in _pillar(strategy, "entry_policy").items():
        if v is not None:
            merged[k] = v
    return merged


# ---- Filtration pillar -------------------------------------------------------
def resolve_entry_filters(strategy, *, global_filters=None) -> dict:
    """Effective entry_filters config for this scope: the strategy's block if it
    has one, else the global `entry_filters` setting. So trend-alignment (#48/#79)
    and future rules can be tuned per (account, source)."""
    ef = _pillar(strategy, "entry_filters")
    return ef if ef else dict(global_filters or {})


def apply_filter_rules(rules, ctx) -> tuple:
    """Evaluate the extensible filtration rules against a trade CONTEXT (#84).

    Each rule: {enabled, name, when:{type, ...}, action:'skip'|'scale', factor}.
    Returns (size_factor, skip, reasons). Rules compose multiplicatively for
    'scale'; any matched 'skip' wins. A rule whose condition inputs are missing
    from ctx is a no-op (fail-open) — so rules needing entry-time features simply
    don't fire until those features are wired. Currently understood conditions:
      session_in {sessions:[...]}          ctx['session'] in list
      always                               unconditional (baseline scaling)
    Structure/regime/bayesian conditions are declared here as they're added."""
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
        # (structure/regime/bayesian conditions plug in here — no-op until wired)
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
