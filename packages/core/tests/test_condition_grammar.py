"""The shared, composable condition grammar (#184).

ONE GRAMMAR, TWO USES. `entry_filters` asks a condition "should I filter this
signal?"; a `generator:rules` backtest asks the SAME expression, bar by bar,
"should I emit one?". This suite pins the properties that make sharing it safe:

  * `all` / `any` / `not` compose over the EXISTING leaves — no new leaf types,
    no second evaluator, and the filtration entry point and the generator entry
    point agree on every case (`test_both_entry_points_agree_*`).
  * THREE-VALUED, which is the whole design. "we could not tell" is not "it did
    not hold": two-valued logic would make `{"not": X}` fire whenever X's inputs
    were absent, i.e. a generator trading on the absence of evidence. Kleene
    logic instead keeps UNKNOWN infectious through `not`, and `all`/`any` only
    resolve when the answer cannot change.
  * FAIL-OPEN SURVIVES COMPOSITION. An unknown indicator, an absent field, an
    unparseable bound or an empty `all` never reads as True.
  * A NESTED LEAF STILL DECLARES ITS INPUTS. An `indicator` buried inside an
    `all` must appear in `ta_rule_requirements`, or the executor computes
    nothing and the rule is silently inert forever.
  * SHADOW SURVIVES COMPOSITION. Wrapping an `indicator` in an `all` must not be
    a way to promote it to live by accident (#167's guardrail).

Pure — no DB, no fixtures beyond a literal ctx.
"""
from beacon_core.execution import strategy as ST

CTX = {
    "price": 2000.0,
    "sessions": ["London"],
    "ta": {
        "15m": {"rsi_14": {"value": 65.0},
                # `cross` is "up"/"down"/None — the values `ta.indicators.macd`
                # actually emits. A hand-built ctx that invents "bull" would
                # make this suite agree with a config the registry can never
                # satisfy (#184).
                "macd_12_26_9": {"macd": 1.5, "signal": 0.4, "cross": "up"},
                "fvg_0.25_50": {"present": True, "bottom": 1990.0, "top": 1995.0},
                "order_block_1.0_50": {"present": False, "bottom": 1980.0}},
        "4h": {"adx_14": {"adx": 31.2, "trending": True}},
    },
    "adx": {"4h": {"adx": 31.2, "trending": True}},
    # Wed 2026-08-12, 08:30 UTC — inside the {07,08,11,12}Z bucket #214 screens.
    "ts": "2026-08-12T08:30:00Z",
}

RSI_LT_70 = {"type": "indicator", "id": "rsi", "timeframe": "15m",
             "field": "value", "op": "lt", "value": 70}
RSI_GT_70 = {"type": "indicator", "id": "rsi", "timeframe": "15m",
             "field": "value", "op": "gt", "value": 70}
MACD_CROSS_UP = {"type": "indicator", "id": "macd", "timeframe": "15m",
             "field": "cross", "op": "eq", "value": "up"}
FVG_PRESENT = {"type": "indicator", "id": "fvg", "timeframe": "15m",
               "field": "present", "op": "is_true"}
OB_PRESENT = {"type": "indicator", "id": "order_block", "timeframe": "15m",
              "field": "present", "op": "is_true"}
# References an indicator that is not in ctx at all -> UNKNOWN, not False.
UNKNOWABLE = {"type": "indicator", "id": "ichimoku", "timeframe": "15m",
              "field": "tenkan", "op": "gt", "value": 1}
IN_LONDON = {"type": "session_in", "sessions": ["London"]}
MORNING_WINDOW = {"type": "time_window", "from": "07:00", "to": "09:00"}
AFTERNOON_WINDOW = {"type": "time_window", "from": "13:00", "to": "15:00"}
IN_NEW_YORK = {"type": "session_in", "sessions": ["New York"]}


# --- the leaves still behave exactly as they did ------------------------------
def test_a_leaf_is_still_a_leaf():
    assert ST.matches_condition(RSI_LT_70, CTX) is True
    assert ST.matches_condition(RSI_GT_70, CTX) is False
    assert ST.matches_condition(MACD_CROSS_UP, CTX) is True
    assert ST.matches_condition(IN_LONDON, CTX) is True
    assert ST.matches_condition(IN_NEW_YORK, CTX) is False


def test_both_entry_points_agree_on_every_leaf():
    """`_matches` is what filtration calls; `matches_condition` is what the
    generator calls. If they can ever disagree, the shared grammar is a fiction
    and the backtest is measuring a different rule from the one that would run."""
    for cond in (RSI_LT_70, RSI_GT_70, MACD_CROSS_UP, FVG_PRESENT, OB_PRESENT,
                 UNKNOWABLE, IN_LONDON, IN_NEW_YORK, MORNING_WINDOW,
                 AFTERNOON_WINDOW, {"type": "always"},
                 {"type": "nonsense"}, {}):
        assert ST._matches(cond, CTX) is ST.matches_condition(cond, CTX)


def test_both_entry_points_agree_on_composed_conditions():
    for cond in ({"all": [RSI_LT_70, MACD_CROSS_UP]},
                 {"any": [RSI_GT_70, OB_PRESENT]},
                 {"not": IN_NEW_YORK},
                 {"all": [RSI_LT_70, {"any": [FVG_PRESENT, OB_PRESENT]}]}):
        assert ST._matches(cond, CTX) is ST.matches_condition(cond, CTX)


# --- composition --------------------------------------------------------------
def test_all_requires_every_arm():
    assert ST.matches_condition({"all": [RSI_LT_70, MACD_CROSS_UP]}, CTX) is True
    assert ST.matches_condition({"all": [RSI_LT_70, RSI_GT_70]}, CTX) is False


def test_any_requires_one_arm():
    assert ST.matches_condition({"any": [RSI_GT_70, FVG_PRESENT]}, CTX) is True
    assert ST.matches_condition({"any": [RSI_GT_70, OB_PRESENT]}, CTX) is False


def test_not_inverts_a_known_answer():
    assert ST.matches_condition({"not": IN_NEW_YORK}, CTX) is True
    assert ST.matches_condition({"not": IN_LONDON}, CTX) is False


def test_the_issue_example_composes_to_a_single_expression():
    """The shape #184 states, end to end: an `all` of two indicator bounds, an
    `any` of two structure predicates, and a session exclusion."""
    cond = {"all": [MACD_CROSS_UP, RSI_LT_70,
                    {"any": [FVG_PRESENT, OB_PRESENT]},
                    {"not": IN_NEW_YORK}]}
    assert ST.matches_condition(cond, CTX) is True


def test_composition_nests_to_any_depth():
    deep = {"all": [{"any": [{"all": [{"not": RSI_GT_70}, MACD_CROSS_UP]}]}]}
    assert ST.matches_condition(deep, CTX) is True


# --- three-valued logic: the reason `not` is safe ------------------------------
def test_an_unreadable_input_is_unknown_not_false():
    assert ST.evaluate_condition(UNKNOWABLE, CTX) is None
    assert ST.matches_condition(UNKNOWABLE, CTX) is False


def test_not_over_a_missing_input_does_not_fire():
    """The failure this whole design exists to prevent. Two-valued logic would
    read the unknown as False and `not` it into True — a generator emitting a
    signal BECAUSE it could not compute the indicator."""
    assert ST.evaluate_condition({"not": UNKNOWABLE}, CTX) is None
    assert ST.matches_condition({"not": UNKNOWABLE}, CTX) is False


def test_all_is_unknown_when_an_arm_is_unknown_and_none_is_false():
    assert ST.evaluate_condition({"all": [RSI_LT_70, UNKNOWABLE]}, CTX) is None
    # ...but a definite False settles it regardless of what else is unknown.
    assert ST.evaluate_condition({"all": [RSI_GT_70, UNKNOWABLE]}, CTX) is False


def test_any_is_unknown_only_while_the_answer_could_still_change():
    assert ST.evaluate_condition({"any": [FVG_PRESENT, UNKNOWABLE]}, CTX) is True
    assert ST.evaluate_condition({"any": [RSI_GT_70, UNKNOWABLE]}, CTX) is None
    assert ST.evaluate_condition({"any": [RSI_GT_70, OB_PRESENT]}, CTX) is False


def test_an_empty_composite_is_not_vacuous_truth():
    """An unfinished rule must not match everything."""
    for cond in ({"all": []}, {"any": []}, {"all": None}, {"any": "nope"}):
        assert ST.evaluate_condition(cond, CTX) is None
        assert ST.matches_condition(cond, CTX) is False


def test_an_empty_context_never_matches_anything():
    for cond in (RSI_LT_70, MACD_CROSS_UP, {"all": [RSI_LT_70]}, {"any": [RSI_LT_70]},
                 {"not": RSI_LT_70}, IN_LONDON):
        assert ST.matches_condition(cond, {}) is False


def test_an_unparseable_bound_is_unknown_through_composition():
    bad = {"type": "indicator", "id": "rsi", "timeframe": "15m",
           "field": "value", "op": "lt", "value": ""}
    assert ST.evaluate_condition(bad, CTX) is None
    assert ST.matches_condition({"not": bad}, CTX) is False
    assert ST.evaluate_condition({"all": [RSI_LT_70, bad]}, CTX) is None


def test_a_composite_key_wins_over_a_stray_type():
    assert ST.matches_condition({"type": "always", "all": [RSI_GT_70]}, CTX) is False


# --- a nested leaf still declares what it needs computed ----------------------
def test_a_nested_indicator_is_still_a_requirement():
    """If it isn't, the executor computes nothing for it, `ctx['ta']` is empty,
    the leaf is UNKNOWN on every evaluation, and the rule is inert forever with
    no error anywhere."""
    rules = [{"enabled": True, "when": {"all": [MACD_CROSS_UP,
                                                {"any": [FVG_PRESENT, OB_PRESENT]}]}}]
    keys = {(r["timeframe"], r["id"]) for r in ST.ta_rule_requirements(rules)}
    assert ("15m", "macd") in keys
    assert ("15m", "fvg") in keys
    assert ("15m", "order_block") in keys


def test_condition_requirements_is_the_generators_entry_point():
    reqs = ST.condition_requirements({"all": [MACD_CROSS_UP, RSI_LT_70]}, "15m")
    assert {r["id"] for r in reqs} == {"macd", "rsi"}
    assert {r["timeframe"] for r in reqs} == {"15m"}


def test_requirements_are_deduped_across_arms():
    cond = {"any": [RSI_LT_70, RSI_GT_70]}
    assert len(ST.condition_requirements(cond, "15m")) == 1


def test_an_unknown_indicator_produces_no_requirement():
    """Silently, and that is why `check --config` prints the resolved list."""
    cond = {"type": "indicator", "id": "not_an_indicator", "timeframe": "15m",
            "field": "value", "op": "gt", "value": 1}
    assert ST.condition_requirements(cond, "15m") == []


def test_a_nested_adx_regime_still_declares_its_timeframe():
    rules = [{"enabled": True,
              "when": {"all": [{"type": "adx_regime", "timeframe": "1h",
                                "trending": True}]}}]
    assert ST.adx_rule_timeframes(rules) == {"1h"}


def test_a_nested_shadow_block_is_still_declared():
    rules = [{"enabled": True,
              "when": {"not": {"type": "turtle_signal", "agrees": True}}}]
    assert ST.shadow_rule_inputs(rules) == {"turtle"}


def test_condition_leaves_walks_to_the_leaves_in_order():
    cond = {"all": [MACD_CROSS_UP, {"any": [FVG_PRESENT, {"not": OB_PRESENT}]}]}
    assert [l["id"] for l in ST.condition_leaves(cond)] == \
        ["macd", "fvg", "order_block"]


# --- shadow-by-default survives composition -----------------------------------
def test_wrapping_an_indicator_does_not_promote_it_to_live():
    """#167's guardrail: gating over a 45-entry registry manufactures
    significant-looking rules by chance, so an authored indicator rule ships
    inert. A composite has no `type` of its own to read — reading nothing must
    not mean 'live'."""
    assert ST.rule_mode({"when": {"all": [MACD_CROSS_UP]}}) == "shadow"
    assert ST.rule_mode({"when": {"any": [IN_LONDON, MACD_CROSS_UP]}}) == "shadow"
    assert ST.rule_mode({"when": {"all": [IN_LONDON]}}) == "live"
    assert ST.rule_mode({"mode": "live", "when": {"all": [MACD_CROSS_UP]}}) == "live"


# --- filtration behaviour is unchanged by the split ---------------------------
def test_a_composed_live_rule_skips_exactly_when_the_expression_holds():
    rules = [{"enabled": True, "name": "composed", "mode": "live",
              "action": "skip",
              "when": {"all": [RSI_LT_70, {"not": IN_NEW_YORK}]}}]
    factor, skip, reasons = ST.apply_filter_rules(rules, CTX)
    assert skip is True and reasons == ["composed"] and factor == 1.0
    assert ST.apply_filter_rules(rules, {})[1] is False


def test_a_composed_rule_that_cannot_be_evaluated_never_skips():
    """The money property: a filtration rule must not be able to delete a signal
    by being wrong about its own inputs (#164)."""
    rules = [{"enabled": True, "name": "c", "mode": "live", "action": "skip",
              "when": {"not": {"all": [UNKNOWABLE]}}}]
    assert ST.apply_filter_rules(rules, CTX)[1] is False


# ---- time_window (#214) ------------------------------------------------------
# Time-of-day carries -0.1455R on the live control book and `session_in` cannot
# express it: a session is a name, and the hours that separate ({07,08,11,12}Z)
# cut across session boundaries.
def _tw(**kw):
    return {"type": "time_window", **kw}


def test_time_window_is_half_open_on_both_ends():
    ctx = {"ts": "2026-08-12T09:00:00Z"}
    assert ST.evaluate_condition(_tw(**{"from": "07:00", "to": "09:00"}), ctx) is False
    assert ST.evaluate_condition(_tw(**{"from": "09:00", "to": "11:00"}), ctx) is True
    # ...so back-to-back windows never both claim the boundary minute
    assert ST.evaluate_condition(_tw(**{"from": "07:00", "to": "09:00"}),
                       {"ts": "2026-08-12T08:59:00Z"}) is True


def test_time_window_crossing_midnight_covers_both_sides():
    night = _tw(**{"from": "22:00", "to": "06:00"})
    for stamp, expected in (("2026-08-12T22:00:00Z", True),
                            ("2026-08-12T23:59:00Z", True),
                            ("2026-08-13T00:00:00Z", True),
                            ("2026-08-13T05:59:00Z", True),
                            ("2026-08-13T06:00:00Z", False),
                            ("2026-08-13T12:00:00Z", False)):
        assert ST.evaluate_condition(night, {"ts": stamp}) is expected, stamp


def test_time_window_days_filter_restricts_the_weekday():
    weekday_morning = _tw(days=["mon", "tue", "wed", "thu", "fri"],
                          **{"from": "07:00", "to": "09:00"})
    assert ST.evaluate_condition(weekday_morning, {"ts": "2026-08-12T08:00:00Z"}) is True
    # 2026-08-15 is a Saturday: same clock time, excluded by the day filter
    assert ST.evaluate_condition(weekday_morning, {"ts": "2026-08-15T08:00:00Z"}) is False
    # a day name we cannot read is UNKNOWN, not a silently-dropped filter
    assert ST.evaluate_condition(_tw(days=["funday"], **{"from": "07:00", "to": "09:00"}),
                       {"ts": "2026-08-12T08:00:00Z"}) is None


def test_time_window_is_unknown_not_false_on_a_missing_or_bad_input():
    """Kleene, exactly as every other leaf: `not` over an unreadable clock must
    stay inert, or a generator trades on the absence of evidence."""
    w = _tw(**{"from": "07:00", "to": "09:00"})
    for ctx in ({}, {"ts": None}, {"ts": ""}, {"ts": "not-a-timestamp"}):
        assert ST.evaluate_condition(w, ctx) is None, ctx
        assert ST.evaluate_condition({"not": w}, ctx) is None, ctx
    stamped = {"ts": "2026-08-12T08:00:00Z"}
    for bad in (_tw(**{"from": "07:00"}), _tw(**{"to": "09:00"}),
                _tw(**{"from": "7am", "to": "9am"}),
                _tw(**{"from": "25:00", "to": "09:00"}),
                _tw(**{"from": "08:00", "to": "08:00"}), _tw()):
        assert ST.evaluate_condition(bad, stamped) is None, bad


def test_time_window_reads_the_zone_it_was_written_in():
    """A window written in a named zone must not be read as UTC, and a zone we
    cannot resolve is UNKNOWN rather than a wrong-by-an-hour gate."""
    ctx = {"ts": "2026-08-12T08:30:00Z"}          # 12:30 in Dubai (UTC+4)
    assert ST.evaluate_condition(_tw(tz="UTC", **{"from": "08:00", "to": "09:00"}), ctx) is True
    dubai = _tw(tz="Asia/Dubai", **{"from": "08:00", "to": "09:00"})
    assert ST.evaluate_condition(dubai, ctx) is False
    assert ST.evaluate_condition(_tw(tz="Asia/Dubai", **{"from": "12:00", "to": "13:00"}),
                                 ctx) is True
    assert ST.evaluate_condition(_tw(tz="Mars/Olympus", **{"from": "08:00", "to": "09:00"}),
                       ctx) is None


def test_time_window_accepts_a_datetime_as_well_as_a_string():
    """The executor puts an aware datetime in ctx; the replay harness puts a bar
    timestamp. Both entry points must read the same clock."""
    import datetime as dt
    aware = dt.datetime(2026, 8, 12, 8, 30, tzinfo=dt.timezone.utc)
    naive = dt.datetime(2026, 8, 12, 8, 30)       # stored UTC, read as UTC
    w = _tw(**{"from": "07:00", "to": "09:00"})
    assert ST.evaluate_condition(w, {"ts": aware}) is True
    assert ST.evaluate_condition(w, {"ts": naive}) is True


def test_time_window_composes_and_agrees_across_both_entry_points():
    """The screened bucket is four disjoint hours, which is `any` over four
    windows — and the same expression has to mean the same thing to the
    generator, or the backtest is measuring a different rule."""
    bad_hours = {"any": [_tw(**{"from": "07:00", "to": "09:00"}),
                         _tw(**{"from": "11:00", "to": "13:00"})]}
    for stamp, expected in (("2026-08-12T07:30:00Z", True),
                            ("2026-08-12T09:30:00Z", False),
                            ("2026-08-12T12:59:00Z", True),
                            ("2026-08-12T13:00:00Z", False)):
        ctx = {"ts": stamp}
        assert ST.evaluate_condition(bad_hours, ctx) is expected, stamp
        assert ST._matches(bad_hours, ctx) is ST.matches_condition(bad_hours, ctx)
    trade_the_rest = {"not": bad_hours}
    assert ST.evaluate_condition(trade_the_rest, {"ts": "2026-08-12T09:30:00Z"}) is True
    assert ST.evaluate_condition(trade_the_rest, {}) is None


def test_a_time_window_rule_needs_no_indicator_computed():
    """It reads the clock, so it must not drag TA fetches onto the entry path."""
    rules = [{"enabled": True, "action": "skip",
              "when": _tw(**{"from": "07:00", "to": "09:00"})}]
    assert ST.ta_rule_requirements(rules) == []
    assert ST.adx_rule_timeframes(rules) == set()
    # and it is a LIVE-by-default leaf, like session_in: nothing to shadow
    assert ST.rule_mode(rules[0]) == "live"


def test_a_time_window_skip_rule_removes_the_signal_it_names():
    rules = [{"enabled": True, "action": "skip", "name": "skip_bad_hours",
              "when": _tw(**{"from": "07:00", "to": "09:00"})}]
    d = ST.evaluate_filter_rules(rules, {"ts": "2026-08-12T08:00:00Z"})
    assert d.skip is True and d.reasons == ["skip_bad_hours"]
    assert ST.evaluate_filter_rules(rules, {"ts": "2026-08-12T10:00:00Z"}).skip is False
    # no clock at all -> fail open, the signal trades (#164)
    assert ST.evaluate_filter_rules(rules, {"price": 2000.0}).skip is False
