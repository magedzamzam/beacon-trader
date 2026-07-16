"""Per-(source, account) execution-policy resolution (#83).

The blocker for a parallel exit-rule A/B was that SL rules lived only on the
source and were shared across every account it fanned out to. This resolves the
EFFECTIVE policy for one (source, account) pair with a strict fallback chain, so
the same signal can run different exit logic per account:

    (source, account) override  ->  source.strategy  ->  global default  ->  built-in

Pure and dependency-light (only the built-in default is imported) so it unit-tests
on a bare box and is safe to call from both the executor (snapshot at entry) and
the monitor (live fallback). Phase 2's entry-rule A/B rides the same mechanism via
the override's `entry_policy` slot.
"""
from __future__ import annotations

from ..strategy.rules import DEFAULT_SL_RULES


def resolve_sl_rules(*, override_rules=None, override_enabled: bool = True,
                     source_rules=None, global_default=None) -> tuple:
    """Return (sl_rules, origin). origin in {'override','source','global','default'}.

    An override only wins when it is enabled AND carries a non-empty rule list —
    a disabled or empty override transparently falls through, so 'no override ⇒
    identical to today' holds. The returned list is a shallow copy so callers may
    snapshot it without aliasing the stored config."""
    if override_rules and override_enabled:
        return list(override_rules), "override"
    if source_rules:
        return list(source_rules), "source"
    if global_default:
        return list(global_default), "global"
    return list(DEFAULT_SL_RULES), "default"


def resolve_entry_ttl(*, override_ttl=None, source_strategy=None):
    """The effective entry-TTL strategy dict for `config.effective_entry_ttl_min`:
    a per-(source,account) override wins, else the source strategy. Returned as a
    strategy-shaped dict so the existing clamp/validation is reused unchanged."""
    if override_ttl is not None:
        return {"entry_ttl_minutes": override_ttl}
    return source_strategy or {}
