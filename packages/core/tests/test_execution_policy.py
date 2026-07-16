"""Per-(source, account) execution-policy resolution (#83) — the fallback chain
that lets the same signal run different exit rules per account. Pure."""
from beacon_core.execution.policy import resolve_sl_rules, resolve_entry_ttl
from beacon_core.strategy.rules import DEFAULT_SL_RULES

OVR = [{"trigger": {"type": "tp_hit", "index": 2}, "action": {"type": "move_sl_to", "target": "entry"}}]
SRC = [{"trigger": {"type": "tp_hit", "index": 1}, "action": {"type": "move_sl_to", "target": "entry"}}]
GLB = [{"trigger": {"type": "price_move", "points": 30}, "action": {"type": "move_sl_to", "target": "entry"}}]


def test_override_wins_when_enabled():
    rules, origin = resolve_sl_rules(override_rules=OVR, override_enabled=True,
                                     source_rules=SRC, global_default=GLB)
    assert rules == OVR and origin == "override"


def test_disabled_or_empty_override_falls_through_to_source():
    # 'no override => identical to today' — disabled or empty override is transparent
    r, o = resolve_sl_rules(override_rules=OVR, override_enabled=False, source_rules=SRC)
    assert r == SRC and o == "source"
    r, o = resolve_sl_rules(override_rules=None, source_rules=SRC)
    assert r == SRC and o == "source"
    r, o = resolve_sl_rules(override_rules=[], override_enabled=True, source_rules=SRC)
    assert r == SRC and o == "source"


def test_source_then_global_then_builtin():
    r, o = resolve_sl_rules(source_rules=None, global_default=GLB)
    assert r == GLB and o == "global"
    r, o = resolve_sl_rules()                    # nothing anywhere -> built-in ladder
    assert r == DEFAULT_SL_RULES and o == "default"


def test_returned_list_is_a_copy():
    r, _ = resolve_sl_rules(override_rules=OVR, override_enabled=True)
    r.append("x")
    assert OVR == [OVR[0]]                        # source list untouched


def test_two_accounts_same_signal_diverge():
    # The A/B invariant: same source rules, but account B has an override -> the
    # two accounts resolve to DIFFERENT ratchets on the identical signal.
    a, _ = resolve_sl_rules(override_rules=None, source_rules=SRC)             # acct A: source default
    b, _ = resolve_sl_rules(override_rules=OVR, override_enabled=True, source_rules=SRC)  # acct B
    assert a != b and a == SRC and b == OVR


def test_entry_ttl_override():
    assert resolve_entry_ttl(override_ttl=15, source_strategy={"entry_ttl_minutes": 60}) \
        == {"entry_ttl_minutes": 15}
    assert resolve_entry_ttl(override_ttl=None, source_strategy={"entry_ttl_minutes": 60}) \
        == {"entry_ttl_minutes": 60}


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok ", n)
    print("ALL PASS")
