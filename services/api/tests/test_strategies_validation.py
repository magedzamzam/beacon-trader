"""Write-side validation for the three execution pillars (#165).

`routers/strategies.py` is what decides which Entry / Filtration / Exit config is
allowed to reach the database, and the executor and monitor read that config on
the live path. It had no test at all, and shipped two defects in 24h:

  * #163 added `mc_probability` / `turtle_signal` to the UI's RULE_TYPES but not
    to `_FILTER_WHEN`, so saving either rule was a 422.
  * #164's `_clean_when` — the layer that stops a blank UI numeric reaching the
    evaluator, where it used to raise and silently delete a signal — shipped with
    hand-verification only.

Both classes are pinned below. Pure functions only: no DB, no TestClient, no
event loop.
"""
import datetime as dt
import inspect
import json
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from beacon_core.analysis import epochs as EP
from beacon_core.execution import ladder as LAD
from beacon_core.execution import strategy as ST
from beacon_core.ta import registry as TA

from app.routers import strategies as S


class _Row:
    """A strategy row, for the pure `_shape` path. A class rather than a Mock so
    a column added to the model without a default shows up here as an
    AttributeError instead of silently rendering as a Mock repr."""

    def __init__(self, **kw):
        self.id = 1
        self.account_id = self.source_id = None
        self.entry_policy = self.entry_filters = self.exit_policy = None
        self.enabled = True
        self.label = self.note = None
        self.version = 1
        self.updated_at = None
        self.epoch_digest = None
        self.epoch_started_at = None
        self.__dict__.update(kw)


ADX = {"rules": [{"name": "skip_adx_trending_1h", "enabled": True,
                  "action": "skip",
                  "when": {"type": "adx_regime", "trending": True,
                           "timeframe": "1h"}}]}

REPO_ROOT = Path(__file__).resolve().parents[3]
RULE_TYPES_JSX = REPO_ROOT / "frontend/src/components/EntryFilterRules.jsx"

# `trend_alignment` is offered by the UI but deliberately NOT accepted into
# `entry_filters.rules`: Strategies.jsx converts it into the legacy
# entry_filters.trend_alignment block before saving, and apply_filter_rules has
# no case for the type — so storing one would be a permanently silent no-op.
UI_ONLY_TYPES = {"trend_alignment"}
# `always` is an evaluator-level baseline; there is no UI affordance for it.
EVALUATOR_ONLY_TYPES = {"always"}


def _rule_type_keys() -> set:
    """Top-level keys of `RULE_TYPES` in EntryFilterRules.jsx.

    Brace-matched rather than indentation-matched so reformatting the JSX does
    not quietly empty this set (an empty set would make the sync test vacuous)."""
    src = RULE_TYPES_JSX.read_text(encoding="utf-8")
    start = src.index("export const RULE_TYPES = {") + src[src.index("export const RULE_TYPES = {"):].index("{")
    depth, i, keys, n = 0, start, set(), len(src)
    while i < n:
        ch = src[i]
        if ch in "\"'`":                      # skip string literals wholesale
            quote, i = ch, i + 1
            while i < n and src[i] != quote:
                i += 2 if src[i] == "\\" else 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1:
            m = re.match(r"\s*(\w+)\s*:", src[i:])
            if m:
                keys.add(m.group(1))
                i += m.end() - 1
        i += 1
    return keys


# ---------------------------------------------------------------- rule types
def test_jsx_rule_types_are_parseable():
    """Guard the guard: if this returns nothing, the sync test below proves
    nothing."""
    keys = _rule_type_keys()
    assert len(keys) >= 3
    assert {"adx_regime", "session_in"} <= keys


def test_every_ui_rule_type_is_accepted_by_the_api():
    """#163's miss: a RULE_TYPES entry with no `_FILTER_WHEN` counterpart is a
    422 the moment anyone tries to save it from the Strategies screen."""
    missing = _rule_type_keys() - UI_ONLY_TYPES - S._FILTER_WHEN
    assert not missing, (
        f"EntryFilterRules.jsx offers {sorted(missing)} but strategies.py "
        f"_FILTER_WHEN rejects them — saving one returns 422")


def test_api_accepts_nothing_the_ui_cannot_produce():
    """The other direction: an accepted type with no UI affordance and no
    evaluator would be dead config."""
    extra = S._FILTER_WHEN - EVALUATOR_ONLY_TYPES - _rule_type_keys()
    assert not extra, f"_FILTER_WHEN accepts {sorted(extra)} which the UI never emits"


def test_trend_alignment_stays_out_of_the_rules_list():
    """It round-trips through the legacy block instead; accepting it here would
    store a rule the evaluator silently ignores forever."""
    assert "trend_alignment" not in S._FILTER_WHEN


# ---------------------------------------------------------------- _clean_when
def test_clean_when_drops_blank_fields():
    """#164: the UI stores an untouched optional numeric as "", which reached
    float("") in the executor's entry path and deleted the signal."""
    out = S._clean_when({"type": "adx_regime", "timeframe": "4h",
                         "trending": True, "min_adx": "", "max_adx": ""})
    assert out == {"type": "adx_regime", "timeframe": "4h", "trending": True}


@pytest.mark.parametrize("value", [0, 0.0, False, [], "0"])
def test_clean_when_preserves_falsy_but_real_values(value):
    """The regression this file exists to catch: `if v` instead of `if v != ""`
    would drop every one of these, and a `min_adx: 0` bound would vanish."""
    assert S._clean_when({"min_adx": value}) == {"min_adx": value}


def test_clean_when_leaves_a_clean_block_untouched():
    when = {"type": "adx_regime", "timeframe": "4h", "trending": True, "min_adx": 30}
    assert S._clean_when(when) == when


# ------------------------------------------------------- _clean_entry_filters
def _rule(when, action="skip"):
    return {"enabled": True, "when": when, "action": action}


def test_entry_filters_strips_blanks_inside_rules():
    ef = {"rules": [_rule({"type": "adx_regime", "timeframe": "4h",
                           "trending": True, "min_adx": ""})]}
    out = S._clean_entry_filters(ef)
    assert "min_adx" not in out["rules"][0]["when"]
    assert out["rules"][0]["action"] == "skip"       # the rest of the rule survives
    assert out["rules"][0]["enabled"] is True


def test_entry_filters_does_not_mutate_the_caller_payload():
    when = {"type": "adx_regime", "timeframe": "4h", "trending": True, "min_adx": ""}
    ef = {"rules": [_rule(when)]}
    S._clean_entry_filters(ef)
    assert when["min_adx"] == ""                     # request body untouched


@pytest.mark.parametrize("t", ["mc_probability", "turtle_signal"])
def test_a_rule_that_can_never_be_evaluated_is_refused(t):
    """Inverts #163, which added these to _FILTER_WHEN so a UI that offered them
    would stop 422-ing. The UI no longer offers them, because neither can be
    evaluated: nothing supplies the `montecarlo` / `turtle` ctx their evaluators
    read — not the executor (which builds the filter ctx from sessions, price,
    ts, adx and ta) and not replay — and strategy.shadow_rule_inputs(), the
    helper that would fetch them, has no caller at all.

    So the rule reads UNKNOWN forever: it can never gate a trade, and it never
    records a shadow measurement either, because there is no value to record.
    Storing one is storing a permanently silent no-op, which is the same failure
    #164 fixed for trend_alignment. Nothing live used either type (0 rows).

    When the executor puts those blocks in the ctx, re-add them to _FILTER_WHEN
    and to EntryFilterRules.jsx RULE_TYPES, and flip this test back."""
    with pytest.raises(HTTPException) as exc:
        S._clean_entry_filters({"rules": [_rule({"type": t})]})
    assert exc.value.status_code == 422


# ------------------------------------------------------- capture follows config
def test_a_gate_can_only_be_armed_on_a_timeframe_capture_can_fetch():
    """#213: arming a rule now WIDENS capture to whatever it references — but
    only for a timeframe bars can be fetched for. Anything else is a rule whose
    removals could never be reconstructed, so it is refused at write time rather
    than discovered a month later in a weekly."""
    assert set(TA.AVAILABLE_TIMEFRAMES) <= set(TA.TF_RESOLUTION)
    with pytest.raises(HTTPException) as exc:
        S._clean_entry_filters({"rules": [_rule(
            {"type": "indicator", "id": "cci", "timeframe": "3w",
             "field": "value", "op": "gte", "value": 100})]})
    assert exc.value.status_code == 422


def test_the_cci_rule_that_started_this_saves_and_declares_its_capture():
    """The live rule that did the most work in the experiment and about which
    the database could say nothing: it is legal config, and it now announces the
    (indicator, timeframe) capture has to persist."""
    when = {"type": "indicator", "id": "cci", "timeframe": "1h",
            "field": "value", "op": "gte", "value": 100}
    rule = S._clean_entry_filters({"rules": [_rule(when)]})["rules"][0]
    reqs = ST.ta_rule_requirements([rule])
    assert [(r["id"], r["timeframe"]) for r in reqs] == [("cci", "1h")]


# ------------------------------------------------------------- time_window
# #214: hour-of-day is the strongest entry-side effect in the live book and
# `session_in` (a NAME match) cannot express it. The evaluator is fail-open by
# design, so an unreadable window would sit there as a permanently silent rule —
# this is the layer that can afford to say no.
TW = {"type": "time_window", "tz": "UTC", "from": "07:00", "to": "09:00"}


def test_time_window_rule_round_trips():
    out = S._clean_entry_filters({"rules": [_rule(dict(TW))]})
    assert out["rules"][0]["when"] == TW
    # a days filter survives cleaning, and an empty one is dropped as a blank
    out = S._clean_entry_filters({"rules": [_rule({**TW, "days": ["mon", "fri"]})]})
    assert out["rules"][0]["when"]["days"] == ["mon", "fri"]


@pytest.mark.parametrize("when", [
    {**TW, "from": "7am"},                      # unparseable bound
    {**TW, "to": "25:00"},                      # out of range
    {"type": "time_window", "to": "09:00"},     # no `from` at all
    {"type": "time_window", "from": "07:00"},   # no `to` at all
    {**TW, "to": "07:00"},                      # empty window
    {**TW, "days": ["funday"]},                 # unknown day
    {**TW, "days": "mon"},                      # days must be a list
    {**TW, "tz": "Mars/Olympus"},               # unknown zone
])
def test_time_window_rejects_a_window_the_evaluator_could_not_read(when):
    with pytest.raises(HTTPException) as exc:
        S._clean_entry_filters({"rules": [_rule(when)]})
    assert exc.value.status_code == 422


def test_time_window_is_live_by_default_like_the_other_calendar_leaf():
    """It reads the clock — there is no estimate to shadow, and no TA fetch it
    can drag onto the entry path."""
    rule = S._clean_entry_filters({"rules": [_rule(dict(TW))]})["rules"][0]
    assert "mode" not in rule and ST.rule_mode(rule) == "live"
    assert ST.ta_rule_requirements([rule]) == []


@pytest.mark.parametrize("bad", [
    {"rules": [_rule({"type": "no_such_filter"})]},
    {"rules": [_rule({"type": "adx_regime"}, action="delete")]},
    {"rules": [{"enabled": True, "action": "skip"}]},          # no `when`
    {"rules": ["not a dict"]},
    {"rules": "not a list"},
])
def test_entry_filters_rejects_malformed_rules(bad):
    with pytest.raises(HTTPException) as exc:
        S._clean_entry_filters(bad)
    assert exc.value.status_code == 422


def test_entry_filters_passthrough_cases():
    assert S._clean_entry_filters(None) is None
    assert S._clean_entry_filters({}) is None                   # empty -> None
    with pytest.raises(HTTPException):
        S._clean_entry_filters("nope")
    # the legacy trend_alignment block rides alongside `rules` untouched
    ef = {"trend_alignment": {"enabled": False}, "rules": []}
    assert S._clean_entry_filters(ef)["trend_alignment"] == {"enabled": False}


def test_cleaned_filters_are_json_serialisable():
    """The column is JSON — anything that survives cleaning has to round-trip."""
    ef = {"rules": [_rule({"type": "adx_regime", "timeframe": "4h",
                           "trending": True, "min_adx": ""})]}
    assert json.loads(json.dumps(S._clean_entry_filters(ef))) == S._clean_entry_filters(ef)


# ------------------------------------------- generic indicator rules (#167)
# The evaluator is deliberately fail-open: a rule it cannot resolve does not
# match, because a filtration rule must never be able to delete a signal by
# raising. That posture means a typo'd indicator or field would SAVE cleanly and
# then sit there as a permanently silent no-op — the same failure mode
# `trend_alignment` is kept out of `rules` to avoid. This layer is the one that
# can afford to say no, so these tests pin that it does.
def _ind(**kw):
    return {"type": "indicator", "id": "rsi", "timeframe": "1h",
            "field": "value", "op": "gte", "value": 70, **kw}


def test_indicator_rule_round_trips():
    out = S._clean_entry_filters({"rules": [_rule(_ind())]})
    assert out["rules"][0]["when"] == _ind()


def test_indicator_rule_accepts_a_multi_output_field_and_params():
    when = _ind(id="macd", field="cross", op="eq", value="up",
                params={"fast": 12, "slow": 26, "signal": 9})
    assert S._clean_entry_filters({"rules": [_rule(when)]})["rules"][0]["when"] == when


@pytest.mark.parametrize("when", [
    _ind(id="not_an_indicator"),
    _ind(field="not_a_field"),
    _ind(timeframe="3y"),
    _ind(op="approximately"),
    _ind(op="between", value=70),            # needs [lo, hi]
    _ind(op="between", value=[70]),
    _ind(op="outside", value="70,80"),
    _ind(ref="something_else"),              # ref must be 'price' or an object
    _ind(ref=["rsi"]),
    _ind(ref={"id": "not_an_indicator"}),
    _ind(ref={"id": "rsi", "field": "not_a_field"}),
])
def test_indicator_rule_rejects_unresolvable_references(when):
    with pytest.raises(HTTPException) as exc:
        S._clean_entry_filters({"rules": [_rule(when)]})
    assert exc.value.status_code == 422


def test_indicator_rule_accepts_both_ref_forms():
    for ref in ("price", {"id": "bbands", "field": "upper"},
                {"id": "adx", "timeframe": "4h", "field": "adx"}):
        out = S._clean_entry_filters({"rules": [_rule(_ind(op="gt", ref=ref))]})
        assert out["rules"][0]["when"]["ref"] == ref


def test_indicator_rule_error_names_the_offending_rule():
    with pytest.raises(HTTPException) as exc:
        S._clean_entry_filters({"rules": [_rule(_ind()), _rule(_ind(field="nope"))]})
    assert "rules[1]" in exc.value.detail


# ------------------------------------------------------------- rule `mode`
def test_mode_is_validated_and_preserved():
    for mode in ("live", "shadow"):
        out = S._clean_entry_filters({"rules": [{**_rule(_ind()), "mode": mode}]})
        assert out["rules"][0]["mode"] == mode


def test_unknown_mode_is_rejected():
    with pytest.raises(HTTPException) as exc:
        S._clean_entry_filters({"rules": [{**_rule(_ind()), "mode": "sort-of-live"}]})
    assert exc.value.status_code == 422


def test_absent_mode_stays_absent_so_the_evaluator_default_applies():
    """Storing a default here would freeze it: the evaluator's default is
    type-dependent (shadow for `indicator`, live for the types that predate it and
    are already deployed), and that has to stay one decision in one place."""
    out = S._clean_entry_filters({"rules": [_rule(_ind()), {**_rule(_ind()), "mode": ""}]})
    assert "mode" not in out["rules"][0] and "mode" not in out["rules"][1]


def test_a_new_indicator_rule_defaults_to_shadow_end_to_end():
    """The guardrail, checked through the write path the UI actually uses: a rule
    saved with no explicit mode cannot skip or scale a trade."""
    saved = S._clean_entry_filters({"rules": [_rule(_ind())]})
    assert ST.rule_mode(saved["rules"][0]) == "shadow"
    ctx = {"ta": {"1h": {"rsi_14": {"value": 99.0}}}}
    assert ST.apply_filter_rules(saved["rules"], ctx) == (1.0, False, [])


def test_shape_exposes_the_live_vs_shadow_count():
    """#167 requires the number of simultaneous LIVE gates to be visible — each one
    is another multiple comparison against a ~50–100 trade sample."""
    row = _Row(entry_filters={"rules": [_rule(_ind()),                  # shadow
                                        {**_rule(_ind()), "mode": "live"},
                                        {"enabled": True, "action": "skip",
                                         "when": {"type": "adx_regime"}}]})  # live
    assert S._shape(row)["filter_modes"] == {"live": 2, "shadow": 1}


# ------------------------------------------------------------------ epochs (#200)
def test_shape_carries_the_epoch_so_it_is_not_reconstructed_from_updated_at():
    """The removed set accumulates WITHIN an epoch and is tested once, so which
    epoch a row is in has to be readable off the row. Deriving it from
    `updated_at` in a weekly script is how a missed bump mis-assigns a week of
    skips to a filter that was not running."""
    row = _Row(entry_filters=ADX, epoch_digest="abc123",
               epoch_started_at=dt.datetime(2026, 7, 30, 8, 57,
                                            tzinfo=dt.timezone.utc))
    out = S._shape(row)
    assert out["epoch_digest"] == "abc123"
    assert out["epoch_started_at"].startswith("2026-07-30T08:57")
    # The human key is derived from the rules AND carries the stored digest, so
    # it cannot drift from the row the way a hand-typed literal can.
    assert out["epoch"] == "adx_regime@1h+trendingTrue#abc123"


def test_the_route_moves_the_clock_only_on_a_semantic_change():
    """The decision the write path delegates, checked at its three branches.
    A relabel must be free — that is the entire point of the digest."""
    now, opened = "NOW", "OPENED"
    same = EP.epoch_digest(ADX, None)
    assert EP.epoch_transition(same, same, opened, now) == {
        "digest": same, "started_at": opened, "closed": False}
    changed = EP.epoch_digest({"rules": [{**ADX["rules"][0],
                                          "when": {"type": "adx_regime",
                                                   "timeframe": "1h",
                                                   "trending": True,
                                                   "min_adx": 30}}]}, None)
    assert EP.epoch_transition(same, changed, opened, now)["closed"] is True


# -------------------------------------------------------- provenance (#201)
# Flat `when`, because `_clean_entry_filters` requires a top-level `when.type`.
# NOTE: the six live `bt_` rules use the COMPOSED `{"not": {...}}` shape from
# #184, which this validator does not accept — they were written by SQL, not
# through this route. That gap is real but pre-existing and out of scope here;
# the gate below still covers every mined rule the portal can author.
BT_RULE = {"name": "bt_1h_cci_value_gte100", "enabled": True, "action": "skip",
           "mode": "live",
           "when": {"type": "indicator", "id": "cci", "field": "value",
                    "op": "gte", "value": 100, "timeframe": "1h"}}
GOOD_PROV = {"status": "recorded", "replay_run_id": 37,
             "n_candidates_screened": 250,
             "effect_in_sample": {"n": 68, "mean_r": 0.0514},
             "effect_holdout": {"n": 30, "mean_r": 0.0301}}


def test_provenance_survives_the_pillar_clean():
    """It must not be dropped by `_clean_entry_filters` — a provenance block
    that silently vanishes on save is worse than none, because the operator
    believes it was recorded."""
    out = S._clean_entry_filters({"rules": [{**BT_RULE, "provenance": GOOD_PROV}]})
    assert out["rules"][0]["provenance"]["replay_run_id"] == 37
    assert out["rules"][0]["provenance"]["effect_holdout"] == {"n": 30, "mean_r": 0.0301}


def test_a_malformed_provenance_is_a_422_not_a_silent_drop():
    with pytest.raises(HTTPException) as exc:
        S._clean_entry_filters({"rules": [{**BT_RULE,
                                           "provenance": {"status": "maybe"}}]})
    assert exc.value.status_code == 422 and "rules[0]" in exc.value.detail


def test_arming_a_new_mined_rule_with_no_provenance_is_refused():
    with pytest.raises(HTTPException) as exc:
        S._check_promotions([BT_RULE], old_rules=[])
    assert exc.value.status_code == 422 and "records nothing" in exc.value.detail


def test_a_rule_already_running_in_this_exact_form_is_not_re_gated():
    """The gate is on the screen->LIVE step. Re-saving a config that is already
    running cannot make anything worse, and refusing it would trap the operator
    behind six rules that predate provenance entirely — including on a routine
    relabel."""
    assert S._check_promotions([BT_RULE], old_rules=[BT_RULE]) == []


def test_changing_the_threshold_of_a_running_rule_re_opens_the_gate():
    """A different threshold is a different rule, however similar it looks —
    the same reason it is a different epoch (#200)."""
    moved = {**BT_RULE, "when": {**BT_RULE["when"], "value": 120}}
    with pytest.raises(HTTPException):
        S._check_promotions([moved], old_rules=[BT_RULE])


def test_a_shadow_rule_can_always_be_saved():
    """Measurement stays cheap; that is the whole point of shadow mode."""
    assert S._check_promotions([{**BT_RULE, "mode": "shadow"}], old_rules=[]) == []


def test_a_deep_screen_warns_without_blocking():
    armed = {**BT_RULE, "provenance": {**GOOD_PROV,
                                       "effect_holdout": {"n": 11, "mean_r": 0.02}}}
    # n=11 clears the floor of 10, so it saves — and says how thin it is.
    warnings = S._check_promotions([armed], old_rules=[])
    assert any("250 candidates screened" in w for w in warnings)


def test_the_read_surface_puts_the_effects_side_by_side():
    row = _Row(entry_filters={"rules": [{**BT_RULE, "provenance": GOOD_PROV}]})
    prov = S._shape(row)["rule_provenance"]["bt_1h_cci_value_gte100"]
    assert prov["mined"] and prov["armed"]
    assert "in-sample +0.0514" in prov["line"] and "holdout +0.0301" in prov["line"]


def test_a_hand_written_rule_does_not_clutter_the_provenance_surface():
    row = _Row(entry_filters=ADX)
    assert S._shape(row)["rule_provenance"] == {}


def test_the_page_shows_provenance_and_never_lets_it_be_typed_in():
    """A claim about a backtest that an operator can edit by hand is not a
    record of anything. The panel renders it; there is no input bound to it."""
    jsx = RULE_TYPES_JSX.read_text(encoding="utf-8")
    assert "function Provenance(" in jsx and "<Provenance p={r.provenance}" in jsx
    assert "effect_in_sample" in jsx and "effect_holdout" in jsx
    assert "provenance:" not in jsx, "provenance must never be patched from the UI"


def test_a_skip_count_is_deduped_by_signal_not_by_event():
    """One signal fans out to several legs and each can log its own
    `entry_filtered`. Counting rows would push an accumulation past the N>=30
    threshold it is measured against without a single extra removal."""
    src = inspect.getsource(S._skips_since)
    assert "sigs.add" in src and "len(sigs)" in src
    assert 'p.get("reason") != "filtration_skip"' in src


# -------------------------------------------------------- _clean_entry_policy
def test_entry_policy_keeps_only_known_keys():
    out = S._clean_entry_policy({"ttl_minutes": 45, "chase_tolerance_r": 0.25,
                                 "not_a_real_key": "x", "honor_market_hint": True})
    assert out == {"ttl_minutes": 45, "chase_tolerance_r": 0.25, "honor_market_hint": True}


def test_entry_policy_rejects_an_unknown_entry_style():
    with pytest.raises(HTTPException) as exc:
        S._clean_entry_policy({"entry_style": "teleport"})
    assert exc.value.status_code == 422


def test_entry_policy_passthrough_cases():
    assert S._clean_entry_policy(None) is None
    assert S._clean_entry_policy({}) is None
    assert S._clean_entry_policy({"not_a_real_key": 1}) is None   # nothing known -> None
    with pytest.raises(HTTPException):
        S._clean_entry_policy([1, 2, 3])


# ------------------------------------------------------ sl_distance (#249)
def test_sl_distance_is_stored_when_it_is_a_positive_number():
    assert S._clean_entry_policy({"sl_distance": 3})["sl_distance"] == 3.0
    assert S._clean_entry_policy({"sl_distance": "2.5"})["sl_distance"] == 2.5


def test_a_cleared_sl_distance_is_dropped_not_stored_as_empty():
    """The UI sends "" for an untouched optional numeric. That is not None, so it
    slips past an `is not None` filter and reaches the DB — the exact shape that
    put `min_adx: ""` on the live entry path (#164). Empty means OFF."""
    assert S._clean_entry_policy({"sl_distance": ""}) is None
    assert S._clean_entry_policy({"sl_distance": None}) is None
    out = S._clean_entry_policy({"ttl_minutes": 45, "sl_distance": ""})
    assert out == {"ttl_minutes": 45} and "sl_distance" not in out


@pytest.mark.parametrize("bad", [0, -3, "-0.5"])
def test_a_nonpositive_sl_distance_is_refused_at_write_time(bad):
    """A stop of zero distance is a typo, not a configuration. Refusing here beats
    silently ignoring it at execution time, where the operator would see the
    channel's stop and no explanation."""
    with pytest.raises(HTTPException) as exc:
        S._clean_entry_policy({"sl_distance": bad})
    assert exc.value.status_code == 422


def test_an_unparseable_sl_distance_is_refused():
    with pytest.raises(HTTPException) as exc:
        S._clean_entry_policy({"sl_distance": "three dollars"})
    assert exc.value.status_code == 422


# ------------------------------------------------------------ ladder (#250)
# The ladder is GLOBAL — a grid of zone shape x TP count in the `staged_ladders`
# setting, served by /strategies/ladders. It is deliberately NOT an entry_policy
# key: which ladder a signal runs follows from the signal's own shape, not from
# the channel, and a per-strategy copy would sit in the DB looking authoritative.
def test_ladder_is_not_an_entry_policy_key():
    from beacon_core.execution.strategy import ENTRY_POLICY_KEYS
    assert "ladder" not in ENTRY_POLICY_KEYS


def test_a_stale_client_sending_a_ladder_has_it_dropped_not_stored():
    out = S._clean_entry_policy({"ttl_minutes": 45, "ladder": [
        {"when": "signal", "action": "open", "order": "POSITION",
         "level": "ENTRY_FROM", "target": 1}]})
    assert out == {"ttl_minutes": 45}


def test_the_grid_validates_every_cell_and_names_the_bad_one():
    good = {"1": {"3": [{"when": "signal", "action": "open", "order": "position",
                         "level": "entry_from", "target": "1"}]}}
    cleaned = LAD.clean_matrix(good)
    assert cleaned[1][3][0]["order"] == "POSITION"

    with pytest.raises(ValueError) as exc:
        LAD.clean_matrix({"1": {"3": [{"when": "whenever", "action": "open",
                                       "order": "POSITION", "level": "MID",
                                       "target": 1}]}})
    assert "zone 1, 3-TP ladder" in str(exc.value)


def test_only_the_cells_that_differ_from_the_default_are_worth_storing():
    """The PUT stores the DIFF, so the setting records what was deliberately
    changed rather than freezing a copy of every default — which would pin the
    fifteen untouched cells against any future correction to them."""
    unchanged = LAD.DEFAULT_MATRIX[LAD.ZONE_SINGLE][3]
    grid = LAD.matrix_with_defaults({"1": {"3": unchanged}})
    assert grid[LAD.ZONE_SINGLE][3] == unchanged


def test_sl_distance_is_a_known_entry_policy_key():
    """Not in ENTRY_POLICY_KEYS = dropped by the cascade merge, and the setting
    silently does nothing. #249 called this failure mode out by name."""
    from beacon_core.execution.strategy import ENTRY_POLICY_KEYS
    assert "sl_distance" in ENTRY_POLICY_KEYS


# ------------------------------------------------- _valid_sl_rules / exit
_GOOD_SL = [{"trigger": {"type": "tp_hit", "index": 1},
             "action": {"type": "move_sl_to", "target": "entry"}}]


def test_valid_sl_rules_accepts_the_shipped_ladder():
    assert S._valid_sl_rules(None) is True
    assert S._valid_sl_rules([]) is True
    assert S._valid_sl_rules(_GOOD_SL) is True


@pytest.mark.parametrize("rules", [
    [{"trigger": {"type": "moon_phase"}, "action": {"type": "move_sl_to", "target": "entry"}}],
    [{"trigger": {"type": "tp_hit"}, "action": {"type": "close_all", "target": "entry"}}],
    [{"trigger": {"type": "tp_hit"}, "action": {"type": "move_sl_to", "target": "nowhere"}}],
    [{"action": {"type": "move_sl_to", "target": "entry"}}],       # no trigger
    ["not a dict"],
    "not a list",
])
def test_valid_sl_rules_rejects_bad_ladders(rules):
    """These guard the ladder the monitor ratchets real stops against."""
    assert S._valid_sl_rules(rules) is False


def test_exit_policy_rejects_a_bad_ladder_and_keeps_a_good_one():
    with pytest.raises(HTTPException) as exc:
        S._clean_exit_policy({"sl_rules": [{"trigger": {"type": "moon_phase"}}]})
    assert exc.value.status_code == 422
    xp = {"sl_rules": _GOOD_SL, "cancel_pending_on_stop": True}
    assert S._clean_exit_policy(xp) == xp
    assert S._clean_exit_policy(None) is None
    assert S._clean_exit_policy({}) is None
