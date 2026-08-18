"""The generic TA-driven entry filtration gate (#167).

One evaluator over the whole indicator registry, replacing one hand-written
matcher (plus one hand-written executor ctx plumb) per indicator. Two things are
under test and both matter for real money:

  * FAIL-OPEN. A filtration rule must never be able to lose a signal. Every bad
    reference — unknown id, missing timeframe, absent field, unparseable bound —
    reads as "does not match", never as a crash and never as a skip.
  * SHADOW BY DEFAULT. Gating over 45 indicators × 7 timeframes × N fields is a
    false-discovery machine at N≈50–100 correlated trades. A newly authored
    indicator rule is computed and recorded but CANNOT alter a trade until someone
    deliberately sets mode=live.
"""
import pytest

from beacon_core.execution import strategy as ST
from beacon_core.ta import registry as TA


def _rule(when, action="skip", **kw):
    return [{"enabled": True, "name": kw.pop("name", "r"), "when": when,
             "action": action, **kw}]


def _live(when, action="skip", **kw):
    return _rule(when, action, mode="live", **kw)


CTX = {
    "price": 2000.0,
    "ta": {
        "1h": {"rsi_14": {"value": 72.0},
               "macd_12_26_9": {"macd": 1.5, "signal": 0.4, "hist": 1.1, "cross": "up"},
               "bbands_20_2": {"middle": 1990.0, "upper": 2010.0, "lower": 1970.0,
                               "pct_b": 0.75, "above_upper": False}},
        "4h": {"adx_14": {"adx": 31.2, "trending": True},
               "psar_0.02_0.2": {"value": 1950.0, "trend": "up", "above": True}},
    },
}


# ---- operators --------------------------------------------------------------
@pytest.mark.parametrize("op,value,expected", [
    ("gt", 70, True), ("gt", 72, False),
    ("gte", 72, True), ("gte", 72.1, False),
    ("lt", 80, True), ("lt", 72, False),
    ("lte", 72, True), ("lte", 71.9, False),
    ("eq", 72, True), ("eq", 71, False),
    ("ne", 71, True), ("ne", 72, False),
])
def test_numeric_ops(op, value, expected):
    when = {"type": "indicator", "id": "rsi", "timeframe": "1h",
            "field": "value", "op": op, "value": value}
    assert ST.apply_filter_rules(_live(when), CTX)[1] is expected


@pytest.mark.parametrize("op,value,expected", [
    ("between", [70, 80], True), ("between", [10, 20], False),
    ("between", [80, 70], True),                    # bounds accepted either way round
    ("outside", [10, 20], True), ("outside", [70, 80], False),
])
def test_range_ops(op, value, expected):
    when = {"type": "indicator", "id": "rsi", "timeframe": "1h",
            "field": "value", "op": op, "value": value}
    assert ST.apply_filter_rules(_live(when), CTX)[1] is expected


def test_range_ops_need_a_pair():
    for bad in (70, [70], [70, 80, 90], None, "70,80"):
        when = {"type": "indicator", "id": "rsi", "timeframe": "1h",
                "op": "between", "value": bad}
        assert ST.apply_filter_rules(_live(when), CTX)[1] is False


def test_boolean_ops_on_a_boolean_field():
    on = {"type": "indicator", "id": "adx", "timeframe": "4h",
          "field": "trending", "op": "is_true"}
    off = {**on, "op": "is_false"}
    assert ST.apply_filter_rules(_live(on), CTX)[1] is True
    assert ST.apply_filter_rules(_live(off), CTX)[1] is False


def test_eq_on_a_boolean_field_accepts_both_spellings():
    """The UI stores a toggle as a real bool; hand-written config and query params
    arrive as 'true'/'false'. Both must mean the same thing."""
    for value in (True, "true", 1):
        when = {"type": "indicator", "id": "adx", "timeframe": "4h",
                "field": "trending", "op": "eq", "value": value}
        assert ST.apply_filter_rules(_live(when), CTX)[1] is True


def test_eq_on_a_string_field():
    when = {"type": "indicator", "id": "macd", "timeframe": "1h",
            "field": "cross", "op": "eq", "value": "up"}
    assert ST.apply_filter_rules(_live(when), CTX)[1] is True
    assert ST.apply_filter_rules(_live({**when, "value": "down"}), CTX)[1] is False


def test_multi_field_indicator_addresses_the_right_output():
    """One indicator, several outputs — the `field` is what makes the gate generic
    instead of one-matcher-per-indicator."""
    base = {"type": "indicator", "id": "macd", "timeframe": "1h", "op": "gt", "value": 1.0}
    assert ST.apply_filter_rules(_live({**base, "field": "macd"}), CTX)[1] is True
    assert ST.apply_filter_rules(_live({**base, "field": "signal"}), CTX)[1] is False
    assert ST.apply_filter_rules(_live({**base, "field": "hist"}), CTX)[1] is True


def test_unknown_op_is_a_no_op():
    when = {"type": "indicator", "id": "rsi", "timeframe": "1h",
            "op": "approximately", "value": 70}
    assert ST.apply_filter_rules(_live(when), CTX) == (1.0, False, [])


# ---- relative comparisons ---------------------------------------------------
def test_ref_price_compares_against_the_live_price():
    """The price-vs-level gate: is price above the upper Bollinger band?"""
    over = {"type": "indicator", "id": "bbands", "timeframe": "1h",
            "field": "lower", "op": "lt", "ref": "price"}          # 1970 < 2000
    under = {**over, "field": "upper"}                             # 2010 < 2000 -> no
    assert ST.apply_filter_rules(_live(over), CTX)[1] is True
    assert ST.apply_filter_rules(_live(under), CTX)[1] is False


def test_ref_another_indicator_field():
    when = {"type": "indicator", "id": "macd", "timeframe": "1h", "field": "macd",
            "op": "gt", "ref": {"id": "macd", "field": "signal"}}   # 1.5 > 0.4
    assert ST.apply_filter_rules(_live(when), CTX)[1] is True
    flipped = {**when, "field": "signal", "ref": {"id": "macd", "field": "macd"}}
    assert ST.apply_filter_rules(_live(flipped), CTX)[1] is False


def test_ref_crosses_timeframes():
    when = {"type": "indicator", "id": "rsi", "timeframe": "1h", "field": "value",
            "op": "gt", "ref": {"id": "adx", "timeframe": "4h", "field": "adx"}}
    assert ST.apply_filter_rules(_live(when), CTX)[1] is True       # 72 > 31.2


def test_ref_beats_value_and_a_bad_ref_is_a_no_op():
    when = {"type": "indicator", "id": "rsi", "timeframe": "1h", "op": "gt",
            "value": 1, "ref": {"id": "rsi", "timeframe": "1d"}}    # TF not in ctx
    assert ST.apply_filter_rules(_live(when), CTX)[1] is False
    assert ST.apply_filter_rules(_live({**when, "ref": "not_price"}), CTX)[1] is False


# ---- fail-open --------------------------------------------------------------
@pytest.mark.parametrize("ctx", [
    {},                                                    # no ta at all
    {"ta": {}},                                            # empty
    {"ta": {"1h": {}}},                                    # TF present, nothing in it
    {"ta": {"1d": {"rsi_14": {"value": 99.0}}}},           # wrong timeframe
    {"ta": {"1h": {"rsi_14": {"other": 99.0}}}},           # wrong field
    {"ta": {"1h": {"rsi_14": {"value": None}}}},           # field present but null
    {"ta": {"1h": {"rsi_14": "not-a-dict"}}},              # malformed block
])
def test_missing_input_never_matches(ctx):
    when = {"type": "indicator", "id": "rsi", "timeframe": "1h",
            "field": "value", "op": "gt", "value": 1}
    assert ST.apply_filter_rules(_live(when), ctx) == (1.0, False, [])


def test_unknown_indicator_id_is_inert():
    when = {"type": "indicator", "id": "does_not_exist", "timeframe": "1h",
            "op": "gt", "value": 1}
    assert ST.apply_filter_rules(_live(when), CTX)[1] is False
    assert ST.ta_rule_requirements(_live(when)) == []


def test_blank_bound_reads_as_absent_not_as_zero():
    """#164's lesson, generalised: the UI stores a cleared numeric as "". A bound
    of "" must mean "no bound", not "compare against 0" — which for `gt` would
    match nearly everything and skip nearly every trade."""
    when = {"type": "indicator", "id": "rsi", "timeframe": "1h",
            "field": "value", "op": "gt", "value": ""}
    assert ST.apply_filter_rules(_live(when), CTX) == (1.0, False, [])
    junk = {**when, "value": "not-a-number"}
    assert ST.apply_filter_rules(_live(junk), CTX) == (1.0, False, [])


def test_an_evaluator_error_cannot_escape_as_an_exception():
    """Whatever garbage is stored, evaluation returns a decision. The executor's
    consumer loop swallows exceptions AFTER the signal leaves the durable queue,
    so a raise here would silently delete a trade (#164)."""
    for junk in ([{"when": None}], [{"when": {"type": "indicator"}}],
                 [{"when": {"type": "indicator", "id": None, "op": None}}],
                 [{"when": {"type": "indicator", "id": "rsi", "op": "gt",
                            "value": {"nested": "object"}}}],
                 ["not-a-rule"], [None]):
        assert ST.apply_filter_rules(junk, CTX) == (1.0, False, [])


def test_omitted_timeframe_resolves_only_when_unambiguous():
    one_tf = {"ta": {"1h": {"rsi_14": {"value": 72.0}}}}
    when = {"type": "indicator", "id": "rsi", "field": "value", "op": "gt", "value": 70}
    assert ST.apply_filter_rules(_live(when), one_tf)[1] is True
    assert ST.apply_filter_rules(_live(when), CTX)[1] is False      # 2 TFs -> no-op


def test_params_select_the_instance_and_default_to_the_registry_default():
    """A rule addresses `ema`; the executor writes `ema_50` / `ema_200`. Omitting
    `params` is not "any EMA" — it is the registry default (period 50), resolved to
    an exact key on both sides so the rule and the feature can't disagree."""
    two = {"ta": {"1h": {"ema_50": {"value": 100.0}, "ema_200": {"value": 90.0}}}}
    ema = {"type": "indicator", "id": "ema", "timeframe": "1h", "field": "value",
           "op": "gt", "value": 95}
    assert ST.apply_filter_rules(_live(ema), two)[1] is True            # ema_50 = 100
    p200 = {**ema, "params": {"period": 200}}
    assert ST.apply_filter_rules(_live(p200), two)[1] is False          # ema_200 = 90
    assert ST.apply_filter_rules(_live({**p200, "value": 85}), two)[1] is True


def test_instance_lookup_falls_back_but_refuses_to_guess():
    """Belt and braces for a hand-built ctx: a bare id resolves, a single unique
    instance resolves, but two candidates for the same id deliberately resolve to
    nothing — a coin flip on the money path is not acceptable."""
    bare = {"ta": {"1h": {"rsi": {"value": 72.0}}}}
    when = {"type": "indicator", "id": "rsi", "timeframe": "1h", "op": "gt", "value": 70}
    assert ST.apply_filter_rules(_live(when), bare)[1] is True

    unique = {"ta": {"1h": {"rsi_9": {"value": 72.0}}}}                 # not rsi_14
    assert ST.apply_filter_rules(_live(when), unique)[1] is True
    ambiguous = {"ta": {"1h": {"rsi_9": {"value": 72.0}, "rsi_21": {"value": 71.0}}}}
    assert ST.apply_filter_rules(_live(when), ambiguous)[1] is False


# ---- shadow vs live ---------------------------------------------------------
def test_indicator_rules_are_shadow_by_default():
    """The guardrail. A rule that would skip every trade in CTX changes nothing
    until someone opts it into live."""
    when = {"type": "indicator", "id": "rsi", "timeframe": "1h",
            "field": "value", "op": "gt", "value": 70}
    d = ST.evaluate_filter_rules(_rule(when, name="rsi hot"), CTX)
    assert d.skip is False and d.factor == 1.0 and d.reasons == []
    assert d.shadow == [{"name": "rsi hot", "type": "indicator", "mode": "shadow",
                         "matched": True, "action": "skip", "factor": None}]


def test_shadow_scale_does_not_touch_the_size_factor():
    when = {"type": "indicator", "id": "rsi", "timeframe": "1h", "op": "gt", "value": 70}
    d = ST.evaluate_filter_rules(_rule(when, "scale", factor=0.25, name="half"), CTX)
    assert d.factor == 1.0                                  # NOT 0.25
    assert d.shadow[0]["factor"] == 0.25 and d.shadow[0]["matched"] is True


def test_shadow_records_non_matches_too():
    """A gate's non-matches are half the evidence: you cannot compute what it would
    have cost you from the matches alone."""
    when = {"type": "indicator", "id": "rsi", "timeframe": "1h", "op": "lt", "value": 10}
    d = ST.evaluate_filter_rules(_rule(when, name="quiet"), CTX)
    assert d.shadow == [{"name": "quiet", "type": "indicator", "mode": "shadow",
                         "matched": False, "action": "skip", "factor": None}]


def test_live_mode_actually_acts():
    when = {"type": "indicator", "id": "rsi", "timeframe": "1h", "op": "gt", "value": 70}
    d = ST.evaluate_filter_rules(_live(when, name="rsi hot"), CTX)
    assert d.skip is True and d.reasons == ["rsi hot"] and d.shadow == []


def test_disabled_shadow_rule_is_not_even_recorded():
    when = {"type": "indicator", "id": "rsi", "timeframe": "1h", "op": "gt", "value": 70}
    off = [{"enabled": False, "when": when, "action": "skip"}]
    assert ST.evaluate_filter_rules(off, CTX).shadow == []


def test_shadow_can_be_forced_on_a_legacy_rule_type():
    adx = {"type": "adx_regime", "timeframe": "4h", "trending": True}
    ctx = {"adx": {"4h": {"adx": 31.2, "trending": True}}}
    assert ST.apply_filter_rules(_rule(adx), ctx)[1] is True             # live default
    shadowed = _rule(adx, mode="shadow")
    d = ST.evaluate_filter_rules(shadowed, ctx)
    assert d.skip is False and d.shadow[0]["matched"] is True


def test_legacy_rule_types_stay_live_by_default():
    """REGRESSION GUARD. `mode` defaults to shadow for the new indicator gate only.
    Defaulting the pre-existing types to shadow would silently UN-gate every
    already-deployed session_in / adx_regime rule — a live behaviour change
    delivered by a refactor, which is exactly what must not happen."""
    for when, ctx in (
        ({"type": "always"}, {}),
        ({"type": "session_in", "sessions": ["overlap"]}, {"session": "overlap"}),
        ({"type": "adx_regime", "timeframe": "4h", "trending": True},
         {"adx": {"4h": {"adx": 31.2, "trending": True}}}),
        ({"type": "mc_probability", "max_expected_r": 0},
         {"montecarlo": {"expected_r": -0.2}}),
        ({"type": "turtle_signal", "agrees": False}, {"turtle": {"agrees": False}}),
    ):
        assert ST.rule_mode({"when": when}) == "live"
        assert ST.apply_filter_rules(_rule(when), ctx)[1] is True, when["type"]


def test_rule_mode_explicit_wins_both_ways():
    ind = {"when": {"type": "indicator"}}
    assert ST.rule_mode(ind) == "shadow"
    assert ST.rule_mode({**ind, "mode": "live"}) == "live"
    assert ST.rule_mode({"when": {"type": "always"}, "mode": "shadow"}) == "shadow"
    assert ST.rule_mode({"when": {"type": "always"}, "mode": "nonsense"}) == "live"


def test_filter_mode_counts_ignores_disabled_rules():
    rules = [
        {"enabled": True, "when": {"type": "indicator", "id": "rsi"}},          # shadow
        {"enabled": True, "when": {"type": "indicator", "id": "rsi"}, "mode": "live"},
        {"enabled": True, "when": {"type": "adx_regime"}},                      # live
        {"enabled": False, "when": {"type": "adx_regime"}},                     # ignored
    ]
    assert ST.filter_mode_counts(rules) == {"live": 2, "shadow": 1}
    assert ST.filter_mode_counts(None) == {"live": 0, "shadow": 0}


def test_apply_filter_rules_stays_a_three_tuple():
    """`apply_filter_rules` is the narrow live-decision entry point and several
    callers unpack it positionally; the shadow record arrives via the richer
    `evaluate_filter_rules` instead of by widening this."""
    assert ST.apply_filter_rules([], {}) == (1.0, False, [])


# ---- requirements: what the executor has to fetch ---------------------------
def test_requirements_are_empty_without_indicator_rules():
    """The hot path stays free on the default install — no rule referencing TA
    means no bar fetch at all."""
    assert ST.ta_rule_requirements([]) == []
    assert ST.ta_rule_requirements(_rule({"type": "session_in", "sessions": ["x"]})) == []
    assert ST.ta_rule_requirements(_rule({"type": "adx_regime", "timeframe": "4h"})) == []


def test_requirements_carry_the_instance_key_the_executor_will_write():
    reqs = ST.ta_rule_requirements(_rule({"type": "indicator", "id": "rsi",
                                          "timeframe": "1h", "op": "gt", "value": 70}))
    assert reqs == [{"id": "rsi", "params": {"period": 14}, "key": "rsi_14",
                     "outputs": ["value"], "timeframe": "1h"}]


def test_requirements_dedupe_across_rules_and_include_shadow_rules():
    """Two rules on the same instance must not cause two fetches; and a SHADOW rule
    still has to be computed or there is nothing to measure and no path to ever
    promoting it."""
    rules = [
        {"enabled": True, "when": {"type": "indicator", "id": "rsi", "timeframe": "1h",
                                   "op": "gt", "value": 70}},                  # shadow
        {"enabled": True, "mode": "live",
         "when": {"type": "indicator", "id": "rsi", "timeframe": "1h",
                  "op": "lt", "value": 30}},
        {"enabled": True, "when": {"type": "indicator", "id": "rsi", "timeframe": "4h",
                                   "op": "gt", "value": 70}},
    ]
    reqs = ST.ta_rule_requirements(rules)
    assert {(r["key"], r["timeframe"]) for r in reqs} == {("rsi_14", "1h"), ("rsi_14", "4h")}


def test_requirements_split_by_params():
    rules = [
        {"enabled": True, "when": {"type": "indicator", "id": "ema", "timeframe": "4h",
                                   "params": {"period": 50}, "op": "gt", "value": 1}},
        {"enabled": True, "when": {"type": "indicator", "id": "ema", "timeframe": "4h",
                                   "params": {"period": 200}, "op": "gt", "value": 1}},
    ]
    assert {r["key"] for r in ST.ta_rule_requirements(rules)} == {"ema_50", "ema_200"}


def test_requirements_include_the_ref_side():
    """A price-vs-band rule reads two indicators; both have to be fetched or the
    rule is permanently inert."""
    when = {"type": "indicator", "id": "rsi", "timeframe": "1h", "op": "gt",
            "ref": {"id": "adx", "timeframe": "4h", "field": "adx"}}
    reqs = ST.ta_rule_requirements(_rule(when))
    assert {(r["id"], r["timeframe"]) for r in reqs} == {("rsi", "1h"), ("adx", "4h")}


def test_requirements_default_and_reject_timeframes():
    when = {"type": "indicator", "id": "rsi", "op": "gt", "value": 70}
    assert ST.ta_rule_requirements(_rule(when))[0]["timeframe"] == ST.DEFAULT_RULE_TIMEFRAME
    assert ST.ta_rule_requirements(_rule(when), default_timeframe="1h")[0]["timeframe"] == "1h"
    bogus = {**when, "timeframe": "3y"}
    assert ST.ta_rule_requirements(_rule(bogus)) == []


def test_requirements_skip_disabled_rules():
    when = {"type": "indicator", "id": "rsi", "timeframe": "1h", "op": "gt", "value": 70}
    assert ST.ta_rule_requirements([{"enabled": False, "when": when}]) == []


def test_ctx_built_the_way_the_executor_builds_it_resolves_the_rule():
    """The one integration risk worth pinning without the executor's import stack:
    the rule and the feature must agree on the key. `_ta_ctx` asks
    `ta_rule_requirements` what to compute, hands it to `compute_timeframe`, and
    files the result under `ctx['ta'][tf]` — so this reproduces that pipeline and
    checks the evaluator can actually find what it asked for. A drift here would be
    invisible: every rule would just quietly never match."""
    from beacon_core.ta.features import compute_timeframe

    bars = [{"o": 100 + i * 0.5, "h": 101 + i * 0.5, "l": 99 + i * 0.5,
             "c": 100.5 + i * 0.5, "v": 10} for i in range(120)]
    rules = [
        {"enabled": True, "mode": "live", "name": "rsi hot", "action": "skip",
         "when": {"type": "indicator", "id": "rsi", "timeframe": "1h",
                  "field": "value", "op": "gt", "value": 60}},
        {"enabled": True, "mode": "live", "name": "ema200", "action": "scale",
         "factor": 0.5,
         "when": {"type": "indicator", "id": "ema", "timeframe": "1h",
                  "params": {"period": 200}, "field": "value", "op": "gt",
                  "value": 0}},
    ]
    reqs = ST.ta_rule_requirements(rules)
    assert {r["key"] for r in reqs} == {"rsi_14", "ema_200"}

    feats = compute_timeframe(bars, None,
                              [{"id": r["id"], "params": r["params"]} for r in reqs])
    ctx = {"ta": {"1h": {k: v for k, v in feats.items() if not k.startswith("_")}}}
    assert set(ctx["ta"]["1h"]) == {"rsi_14"}          # 120 bars is short of EMA-200
    assert ST.apply_filter_rules(rules, ctx)[1] is True   # a steady ramp -> RSI 100

    # ...and the EMA rule is simply inert while its input is unavailable, rather
    # than scaling risk off a value that was never computed.
    assert ST.apply_filter_rules(rules[1:], ctx)[0] == 1.0


def test_every_registry_indicator_is_gateable_without_new_code():
    """The acceptance criterion: a rule referencing ANY registry id resolves its
    requirement and evaluates, with no per-indicator matcher anywhere."""
    for spec in TA.REGISTRY:
        for field in spec["outputs"]:
            when = {"type": "indicator", "id": spec["id"], "timeframe": "1h",
                    "field": field, "op": "gt", "value": 0}
            reqs = ST.ta_rule_requirements(_rule(when))
            assert len(reqs) == 1, spec["id"]
            # inert against an empty ctx, and never raising
            assert ST.apply_filter_rules(_live(when), {}) == (1.0, False, [])


# ---- what the rule READ, not just what it decided (#213) ---------------------
# The highest-volume live rule in the experiment gated 51 removals on `cci`, and
# nothing in the database could say what value fired it: the skip event recorded
# the rule NAME and `signal_features` has never held a cci reading. A gate we
# cannot observe cannot be reviewed, promoted or retired.
def test_explain_records_the_value_a_firing_rule_actually_read():
    when = {"type": "indicator", "id": "rsi", "timeframe": "1h",
            "field": "value", "op": "gte", "value": 70}
    (leaf,) = ST.explain_condition(when, CTX)
    assert leaf["id"] == "rsi" and leaf["timeframe"] == "1h"
    assert leaf["op"] == "gte" and leaf["value"] == 70
    assert leaf["actual"] == 72.0                 # the reading, not the verdict
    assert leaf["result"] is True


def test_explain_marks_an_input_nobody_captured():
    """`actual: None` with `result: None` IS the signature of #213: a gate
    firing (or silently never firing) on something we do not persist."""
    when = {"type": "indicator", "id": "cci", "timeframe": "1h",
            "field": "value", "op": "gte", "value": 100}
    (leaf,) = ST.explain_condition(when, CTX)
    assert leaf["actual"] is None and leaf["result"] is None


def test_explain_walks_a_composed_condition_leaf_by_leaf():
    cond = {"all": [{"type": "indicator", "id": "rsi", "timeframe": "1h",
                     "field": "value", "op": "gte", "value": 70},
                    {"not": {"type": "adx_regime", "timeframe": "4h",
                             "trending": True}}]}
    ctx = {**CTX, "adx": {"4h": {"adx": 31.2, "trending": True}}}
    leaves = ST.explain_condition(cond, ctx)
    assert [l["type"] for l in leaves] == ["indicator", "adx_regime"]
    assert leaves[1]["actual"] == {"adx": 31.2, "trending": True}
    assert leaves[1]["result"] is True            # the LEAF, not the `not`


def test_explain_is_json_safe_for_every_leaf_type():
    import json
    ctx = {**CTX, "sessions": ["London"], "ts": "2026-08-12T08:30:00Z",
           "montecarlo": {"p_win": 0.6}, "turtle": {"position": "long"}}
    for when in ({"type": "session_in", "sessions": ["London"]},
                 {"type": "time_window", "from": "07:00", "to": "09:00"},
                 {"type": "adx_regime", "timeframe": "4h", "trending": True},
                 {"type": "mc_probability", "min_p_win": 0.5},
                 {"type": "turtle_signal", "agrees": True},
                 {"type": "always"}):
        json.dumps(ST.explain_condition(when, ctx))


def test_the_decision_carries_an_audit_record_for_live_and_shadow_rules():
    rules = (_live({"type": "indicator", "id": "rsi", "timeframe": "1h",
                    "field": "value", "op": "gte", "value": 70}, name="live_rsi")
             + _rule({"type": "indicator", "id": "cci", "timeframe": "1h",
                      "field": "value", "op": "gte", "value": 100}, name="shadow_cci"))
    d = ST.evaluate_filter_rules(rules, CTX)
    assert d.skip is True and d.reasons == ["live_rsi"]     # unchanged shape
    by_name = {e["name"]: e for e in d.evaluated}
    assert set(by_name) == {"live_rsi", "shadow_cci"}
    assert by_name["live_rsi"]["mode"] == "live"
    assert by_name["live_rsi"]["leaves"][0]["actual"] == 72.0
    # the shadow rule is recorded WITH its reading, so it can be screened before
    # anyone argues about arming it
    assert by_name["shadow_cci"]["mode"] == "shadow"
    assert by_name["shadow_cci"]["matched"] is None      # tri-state: unreadable
    assert by_name["shadow_cci"]["leaves"][0]["actual"] is None


def test_a_rule_that_did_not_match_is_recorded_too():
    """A live gate's non-matches are half the evidence for ever keeping it."""
    rules = _live({"type": "indicator", "id": "rsi", "timeframe": "1h",
                   "field": "value", "op": "gte", "value": 99}, name="never")
    d = ST.evaluate_filter_rules(rules, CTX)
    assert d.skip is False and d.reasons == []
    assert d.evaluated[0]["matched"] is False
    assert d.evaluated[0]["leaves"][0]["actual"] == 72.0


def test_a_disabled_rule_is_not_recorded_as_evaluated():
    rules = _live({"type": "indicator", "id": "rsi", "timeframe": "1h",
                   "field": "value", "op": "gte", "value": 70})
    rules[0]["enabled"] = False
    assert ST.evaluate_filter_rules(rules, CTX).evaluated == []


def test_the_audit_record_cannot_change_the_decision():
    """It is a trail, not a second evaluator: same inputs, same skip."""
    rules = _live({"type": "indicator", "id": "rsi", "timeframe": "1h",
                   "field": "value", "op": "gte", "value": 70})
    before = ST.apply_filter_rules(rules, CTX)
    after = ST.evaluate_filter_rules(rules, CTX)
    assert before == (after.factor, after.skip, after.reasons)
