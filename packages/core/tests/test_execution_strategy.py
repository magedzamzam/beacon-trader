"""Per-(account, source) execution-strategy resolution (#84) — the 3-pillar
strategy + scope fallback that unlocks per-account entry/filter/exit A/B. Pure."""
from types import SimpleNamespace as S

from beacon_core.execution import strategy as ST
from beacon_core.strategy.rules import DEFAULT_SL_RULES

OVR = [{"trigger": {"type": "tp_hit", "index": 2}, "action": {"type": "move_sl_to", "target": "entry"}}]
SRC = [{"trigger": {"type": "tp_hit", "index": 1}, "action": {"type": "move_sl_to", "target": "entry"}}]


def strat(account_id, source_id, **pillars):
    return S(account_id=account_id, source_id=source_id, enabled=pillars.pop("enabled", True),
             entry_policy=pillars.get("entry_policy"), entry_filters=pillars.get("entry_filters"),
             exit_policy=pillars.get("exit_policy"))


def test_most_specific_scope_wins():
    rows = [strat(None, None, label="global"), strat(5, None, label="acct"),
            strat(None, 6, label="src"), strat(5, 6, label="exact")]
    assert ST.resolve_strategy(rows, 5, 6).account_id == 5 and ST.resolve_strategy(rows, 5, 6).source_id == 6
    # drop the exact one -> account-scope beats source-scope (2 > 1)
    assert ST.resolve_strategy(rows[:3], 5, 6).account_id == 5
    # only source + global -> source-scope wins
    assert ST.resolve_strategy([rows[0], rows[2]], 5, 6).source_id == 6
    # nothing matches this account/source but global does
    assert ST.resolve_strategy([strat(9, None), strat(None, None)], 5, 6).account_id is None


def test_disabled_and_nonmatching_ignored():
    rows = [strat(5, 6, enabled=False), strat(None, None)]
    assert ST.resolve_strategy(rows, 5, 6).account_id is None      # exact is disabled -> global
    assert ST.resolve_strategy([strat(9, 9)], 5, 6) is None         # nothing matches
    assert ST.resolve_strategy([], 5, 6) is None


def test_exit_pillar_fallback_chain():
    r, o = ST.exit_sl_rules(strat(5, 6, exit_policy={"sl_rules": OVR}), source_rules=SRC)
    assert r == OVR and o == "strategy"
    r, o = ST.exit_sl_rules(strat(5, 6, exit_policy={}), source_rules=SRC)   # no exit pillar
    assert r == SRC and o == "source"
    r, o = ST.exit_sl_rules(None, source_rules=None, global_default=None)
    assert r == DEFAULT_SL_RULES and o == "default"
    r, _ = ST.exit_sl_rules(strat(5, 6, exit_policy={"sl_rules": OVR}))
    r.append("x")
    assert OVR == [OVR[0]]                                           # returns a copy


def test_two_accounts_same_source_diverge():
    # THE A/B invariant: same source, account B has an exit override -> different ladders
    rows = [strat(None, 6, exit_policy={"sl_rules": SRC}), strat(5, 6, exit_policy={"sl_rules": OVR})]
    a, _ = ST.exit_sl_rules(ST.resolve_strategy(rows, 4, 6), source_rules=SRC)   # acct 4 -> source-scope
    b, _ = ST.exit_sl_rules(ST.resolve_strategy(rows, 5, 6), source_rules=SRC)   # acct 5 -> its override
    assert a == SRC and b == OVR and a != b


def test_pillar_cascade_to_any_any_base():
    # #104: a specific row that only sets EXIT must still inherit entry/filtration
    # from the (Any, Any) base — not silently fall back to code defaults.
    base = strat(None, None,
                 entry_policy={"chase_tolerance_r": 0.75, "ttl_minutes": 45},
                 entry_filters={"trend_alignment": {"enabled": True, "require_htf_concordance": True}})
    specific = strat(5, 6, exit_policy={"sl_rules": OVR})      # exit only
    chain = ST.resolve_chain([base, specific], 5, 6)
    assert [s.source_id for s in chain] == [6, None]           # most-specific first

    # exit comes from the specific row...
    assert ST.exit_sl_rules(chain)[0] == OVR
    # ...while entry + filtration cascade down to the base
    ep = ST.entry_policy(chain, global_planner={"chase_tolerance_r": 0.25, "beyond_tolerance": "limit"})
    assert ep["chase_tolerance_r"] == 0.75 and ep["ttl_minutes"] == 45
    assert ep["beyond_tolerance"] == "limit"                   # untouched code default
    ef = ST.resolve_entry_filters(chain)
    assert ef["trend_alignment"]["require_htf_concordance"] is True


def test_specific_row_overrides_only_keys_it_sets():
    base = strat(None, None, entry_policy={"chase_tolerance_r": 0.75, "ttl_minutes": 45})
    specific = strat(5, 6, entry_policy={"ttl_minutes": 15})   # overrides ttl only
    chain = ST.resolve_chain([base, specific], 5, 6)
    ep = ST.entry_policy(chain, global_planner={"chase_tolerance_r": 0.25})
    assert ep["ttl_minutes"] == 15          # specific wins
    assert ep["chase_tolerance_r"] == 0.75  # inherited from the (Any,Any) base


def test_htf_concordance_settable_per_account_source():
    # #104 acceptance: concordance is per-(account, source), not just global.
    base = strat(None, None, entry_filters={"trend_alignment": {"enabled": True, "require_htf_concordance": False}})
    armB = strat(7, 12, entry_filters={"trend_alignment": {"enabled": True,
                                                           "require_htf_concordance": True,
                                                           "htf_timeframe": "1h"}})
    a = ST.resolve_entry_filters(ST.resolve_chain([base, armB], 5, 12))   # acct 5 -> base
    b = ST.resolve_entry_filters(ST.resolve_chain([base, armB], 7, 12))   # acct 7 -> its own
    assert a["trend_alignment"]["require_htf_concordance"] is False
    assert b["trend_alignment"]["require_htf_concordance"] is True
    assert b["trend_alignment"]["htf_timeframe"] == "1h"


def test_entry_policy_merge():
    glob = {"chase_tolerance_r": 0.25, "beyond_tolerance": "limit", "max_tp_distance_pct": 0.5}
    ep = ST.entry_policy(strat(5, 6, entry_policy={"chase_tolerance_r": 0.5, "ttl_minutes": 15}),
                         global_planner=glob, source_ttl=60)
    assert ep["chase_tolerance_r"] == 0.5          # strategy overrides global
    assert ep["ttl_minutes"] == 15                 # strategy overrides source ttl
    assert ep["beyond_tolerance"] == "limit"       # untouched global default
    # no strategy -> global + source ttl only
    ep2 = ST.entry_policy(None, global_planner=glob, source_ttl=60)
    assert ep2["ttl_minutes"] == 60 and ep2["chase_tolerance_r"] == 0.25


def test_cancel_pending_resolution():
    assert ST.cancel_pending_on_stop(strat(5, 6, exit_policy={"cancel_pending_on_stop": False})) is False
    assert ST.cancel_pending_on_stop(None, source_strategy={"cancel_pending_on_stop": False}) is False
    assert ST.cancel_pending_on_stop(None) is True


def test_apply_filter_rules():
    rules = [
        {"enabled": True, "name": "overlap half", "when": {"type": "session_in", "sessions": ["overlap"]}, "action": "scale", "factor": 0.5},
        {"enabled": True, "name": "always 2x", "when": {"type": "always"}, "action": "scale", "factor": 2.0},
        {"enabled": False, "name": "off", "when": {"type": "always"}, "action": "skip"},
    ]
    f, skip, reasons = ST.apply_filter_rules(rules, {"session": "overlap"})
    assert f == 1.0 and skip is False and "overlap half" in reasons and "always 2x" in reasons  # 0.5*2.0
    f2, _, _ = ST.apply_filter_rules(rules, {"session": "london"})                 # overlap rule doesn't match
    assert f2 == 2.0
    # a skip rule wins; missing ctx input is a no-op (fail-open)
    sk = [{"when": {"type": "session_in", "sessions": ["overlap"]}, "action": "skip", "name": "no-news"}]
    assert ST.apply_filter_rules(sk, {"session": "overlap"})[1] is True
    assert ST.apply_filter_rules(sk, {})[1] is False


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok ", n)
    print("ALL PASS")
