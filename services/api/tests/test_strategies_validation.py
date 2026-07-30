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
import json
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routers import strategies as S

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


def test_entry_filters_accepts_the_new_shadow_rule_types():
    """#163: these must persist, not 422."""
    for t in ("mc_probability", "turtle_signal"):
        out = S._clean_entry_filters({"rules": [_rule({"type": t})]})
        assert out["rules"][0]["when"]["type"] == t


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
