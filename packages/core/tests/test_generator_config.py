"""An engine strategy is validated before it is saved (#224).

The condition grammar is FAIL-OPEN on purpose: a leaf whose input is missing is
UNKNOWN and never fires. That is right for a filter (it must not block a trade
because an indicator was unavailable) and dangerous for a generator, because a
strategy with a typo does not raise - it emits nothing, forever, and looks
exactly like a strategy that simply had no setups. Since the portal is where
strategies get authored, the portal is where they have to be checked.
"""
from beacon_core.generator.config import (
    KIND_ENGINE, SOURCE_KINDS, engine_config, validate_generator_config)

# The config behind replay run 43 (+0.241R on a window it had never seen).
RUN43 = {
    "timeframe": "15m",
    "long": {"when": {"all": [
        {"type": "indicator", "id": "macd", "timeframe": "15m",
         "field": "cross", "op": "eq", "value": "up"},
        {"type": "indicator", "id": "rsi", "timeframe": "15m",
         "field": "value", "op": "lt", "value": 70},
        {"any": [
            {"type": "indicator", "id": "fvg", "timeframe": "15m",
             "field": "present", "op": "is_true"},
            {"type": "indicator", "id": "order_block", "timeframe": "15m",
             "field": "present", "op": "is_true"}]}]}},
    "short": {"when": {"all": [
        {"type": "indicator", "id": "macd", "timeframe": "15m",
         "field": "cross", "op": "eq", "value": "down"},
        {"type": "indicator", "id": "rsi", "timeframe": "15m",
         "field": "value", "op": "gt", "value": 30}]}},
    "entry": {"type": "close"},
    "sl": {"type": "atr_mult", "timeframe": "1h", "period": 14, "mult": 1.5},
    "tps": [{"type": "r_mult", "r": 1.0}, {"type": "r_mult", "r": 2.0},
            {"type": "r_mult", "r": 3.0}],
    "cooldown_bars": 60,
    "max_signals_per_day": 8,
}


def _without(key):
    c = dict(RUN43)
    c.pop(key)
    return c


def test_the_strategy_we_actually_measured_is_valid():
    """If the validator rejects the config that produced the evidence, the
    validator is wrong, not the config."""
    assert validate_generator_config(RUN43) == []


def test_engine_is_a_source_kind():
    assert KIND_ENGINE in SOURCE_KINDS
    assert set(SOURCE_KINDS) >= {"telegram", "tradingview", "manual", "api"}


# --- the caps, which are the safety property --------------------------------

def test_caps_are_required():
    """A condition true for 50 consecutive bars emits 50 signals and the risk
    caps then decide the strategy - you would be measuring
    max_open_risk_per_symbol, not the indicator (#169)."""
    for key in ("cooldown_bars", "max_signals_per_day"):
        errs = validate_generator_config(_without(key))
        assert any(key in e and "required" in e for e in errs), errs


def test_absurd_caps_are_refused():
    assert validate_generator_config({**RUN43, "max_signals_per_day": 5000})
    assert validate_generator_config({**RUN43, "cooldown_bars": 0})


# --- the grammar ------------------------------------------------------------

def test_unknown_indicator_is_named_with_the_known_set():
    errs = validate_generator_config(
        {**RUN43, "long": {"when": {"type": "indicator", "id": "nope",
                                    "op": "gt", "value": 1}}})
    assert errs and "unknown indicator" in errs[0] and "known:" in errs[0]


def test_a_field_the_indicator_does_not_have_is_caught():
    errs = validate_generator_config(
        {**RUN43, "long": {"when": {"type": "indicator", "id": "rsi",
                                    "timeframe": "15m", "field": "banana",
                                    "op": "gt", "value": 1}}})
    assert any("has no field" in e for e in errs), errs


def test_a_comparison_with_nothing_to_compare_against_is_caught():
    errs = validate_generator_config(
        {**RUN43, "long": {"when": {"type": "indicator", "id": "rsi",
                                    "timeframe": "15m", "field": "value",
                                    "op": "gt"}}})
    assert any("needs a `value`" in e for e in errs), errs


def test_a_GATING_leaf_cannot_create_a_signal():
    """`session_in` / `adx_regime` narrow an existing signal. Accepting one as a
    generator condition would store a strategy that can never emit."""
    errs = validate_generator_config(
        {**RUN43, "long": {"when": {"type": "session_in", "sessions": ["NY"]}}})
    assert any("cannot create one" in e for e in errs), errs


def test_composers_recurse():
    errs = validate_generator_config(
        {**RUN43, "long": {"when": {"all": [{"any": [
            {"type": "indicator", "id": "nope", "op": "gt", "value": 1}]}]}}})
    assert any("unknown indicator" in e for e in errs), errs
    assert validate_generator_config({**RUN43, "long": {"when": {"all": []}}})


def test_a_strategy_with_no_side_can_never_fire():
    c = dict(RUN43)
    c.pop("long"); c.pop("short")
    assert any("never emit" in e for e in validate_generator_config(c)), c


# --- geometry ---------------------------------------------------------------

def test_a_signal_needs_a_stop_and_a_target():
    assert any("sl.type" in e for e in validate_generator_config(_without("sl")))
    assert any("tps" in e for e in validate_generator_config(_without("tps")))


def test_targets_must_be_ordered_outward():
    errs = validate_generator_config(
        {**RUN43, "tps": [{"type": "r_mult", "r": 3.0},
                          {"type": "r_mult", "r": 1.0}]})
    assert any("ordered outward" in e for e in errs), errs


def test_a_negative_atr_stop_is_refused():
    errs = validate_generator_config(
        {**RUN43, "sl": {"type": "atr_mult", "mult": -1}})
    assert any("positive" in e for e in errs), errs


# --- every problem at once --------------------------------------------------

def test_all_errors_are_returned_not_just_the_first():
    """The operator fixes five typos once, not five times."""
    broken = {"timeframe": "17m", "entry": {"type": "nope"},
              "sl": {"type": "nope"}, "tps": []}
    errs = validate_generator_config(broken)
    assert len(errs) >= 4, errs


# --- the accessor -----------------------------------------------------------

def test_engine_config_reads_the_generator_block():
    assert engine_config({"generator": RUN43}) == RUN43
    assert engine_config({"sl_rules": []}) == {}
    assert engine_config(None) == {}
