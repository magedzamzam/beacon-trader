"""Capture follows configuration (#213).

Since #167 an `indicator` gate can name ANY registry entry, which made arming a
gate a config act. The capture list did not follow: `signal_features` held 15
hand-maintained families, and the highest-volume live rule in the experiment
gated 51 removals (-19,430 AED of control P&L) on `cci`, of which the database
holds not one value. No counterfactual on the other twelve channels, no
out-of-sample screen, nothing to review.

A rule can now never reference something we do not persist, because the live
rules are themselves an input to what gets persisted.
"""
from beacon_core.execution import strategy as ST
from beacon_core.ta import registry as TA
from beacon_core.ta.capture import capture_plan

CFG = {"timeframes": ["1h", "4h"],
       "indicators": [{"id": "rsi", "params": {"period": 14}}]}

CCI_RULE = {"enabled": True, "action": "skip", "name": "bt_1h_cci_value_gte100",
            "when": {"type": "indicator", "id": "cci", "timeframe": "1h",
                     "field": "value", "op": "gte", "value": 100}}


def _reqs(*rules):
    return ST.ta_rule_requirements(list(rules))


def test_arming_a_rule_widens_capture_to_the_indicator_it_gates_on():
    plan = capture_plan(CFG, _reqs(CCI_RULE))
    assert any(i["id"] == "cci" for i in plan["1h"])
    # ...on THAT timeframe only — the rule asked for 1h, not for everything
    assert not any(i["id"] == "cci" for i in plan["4h"])
    # and the configured set is still captured
    assert any(i["id"] == "rsi" for i in plan["1h"])


def test_a_rule_on_an_unconfigured_timeframe_adds_that_timeframe():
    rule = {**CCI_RULE, "when": {**CCI_RULE["when"], "timeframe": "15m"}}
    plan = capture_plan(CFG, _reqs(rule))
    assert "15m" in plan
    assert any(i["id"] == "cci" for i in plan["15m"])
    # the new timeframe still gets the baseline families, or the rule's own
    # indicator would be the only thing ever known about it
    assert any(i["id"] == "rsi" for i in plan["15m"])


def test_capture_is_unchanged_when_no_rule_references_ta():
    assert capture_plan(CFG, []) == {"1h": CFG["indicators"],
                                     "4h": CFG["indicators"]}


def test_the_same_indicator_is_not_captured_twice():
    plan = capture_plan(CFG, _reqs(CCI_RULE, CCI_RULE))
    assert sum(1 for i in plan["1h"] if i["id"] == "cci") == 1
    rsi = {"enabled": True, "action": "skip",
           "when": {"type": "indicator", "id": "rsi", "timeframe": "1h",
                    "field": "value", "op": "gte", "value": 70}}
    plan = capture_plan(CFG, _reqs(rsi))
    assert sum(1 for i in plan["1h"] if i["id"] == "rsi") == 1


def test_the_same_indicator_on_different_params_is_a_different_capture():
    """`cci(20)` and `cci(50)` land under different instance keys, so a rule on
    one must not be satisfied by the other already being captured."""
    slow = {"enabled": True, "action": "skip",
            "when": {"type": "indicator", "id": "cci", "timeframe": "1h",
                     "params": {"period": 50}, "field": "value",
                     "op": "gte", "value": 100}}
    plan = capture_plan(CFG, _reqs(CCI_RULE, slow))
    periods = sorted(i["params"]["period"] for i in plan["1h"] if i["id"] == "cci")
    assert periods == [20, 50]


def test_a_shadow_rule_widens_capture_too():
    """A rule has to be measurable BEFORE it is armed, or it can never clear the
    promotion bar."""
    shadow = {**CCI_RULE, "mode": "shadow"}
    assert ST.rule_mode(shadow) == "shadow"
    assert any(i["id"] == "cci" for i in capture_plan(CFG, _reqs(shadow))["1h"])


def test_a_nested_rule_declares_its_inputs_like_a_flat_one():
    composed = {"enabled": True, "action": "skip", "name": "composed",
                "when": {"all": [CCI_RULE["when"],
                                 {"type": "session_in", "sessions": ["London"]}]}}
    assert any(i["id"] == "cci" for i in capture_plan(CFG, _reqs(composed))["1h"])


def test_a_timeframe_no_broker_resolution_exists_for_is_not_invented():
    """Fail-safe: capture only claims what it can actually fetch bars for."""
    plan = capture_plan(CFG, [{"id": "cci", "params": {"period": 20},
                               "key": "cci_20", "outputs": ["value"],
                               "timeframe": "3w"}])
    assert "3w" not in plan


def test_every_gateable_timeframe_is_a_capturable_one():
    """The rule vocabulary and the capture fetcher must not drift apart — a
    timeframe a rule may name but capture cannot fetch is a silent measurement
    hole of exactly the class this issue is about."""
    assert set(TA.AVAILABLE_TIMEFRAMES) <= set(TA.TF_RESOLUTION)
