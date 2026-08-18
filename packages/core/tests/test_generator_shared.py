"""The backtest and the live producer share one generator (#224, step 3).

The logic that turns a condition into a priced signal used to live only in the
replay harness. That is fine with one caller and fatal with two: a producer
written against a copy drifts on its first edit, and every backtest number
silently stops describing the thing that trades.

These tests pin the two halves of "cannot drift":

  * the shared brain is in `beacon_core`, and replay's module is a thin adapter
    over it rather than a second implementation;
  * anything the PORTAL will accept (step 2's validator), the GENERATOR will
    also accept (this module's spec) -- otherwise a strategy saves green and
    then refuses to run.
"""
import datetime as dt

import pytest

from beacon_core.generator import rules as G
from beacon_core.generator.config import validate_generator_config

RUN43 = {
    "timeframe": "15m",
    "long": {"when": {"all": [
        {"type": "indicator", "id": "macd", "timeframe": "15m",
         "field": "cross", "op": "eq", "value": "up"},
        {"type": "indicator", "id": "rsi", "timeframe": "15m",
         "field": "value", "op": "lt", "value": 70}]}},
    "short": {"when": {"type": "indicator", "id": "macd", "timeframe": "15m",
                       "field": "cross", "op": "eq", "value": "down"}},
    "entry": {"type": "close"},
    "sl": {"type": "atr_mult", "timeframe": "1h", "period": 14, "mult": 1.5},
    "tps": [{"type": "r_mult", "r": 1.0}, {"type": "r_mult", "r": 2.0},
            {"type": "r_mult", "r": 3.0}],
    "cooldown_bars": 60, "max_signals_per_day": 8,
}


class _Provider:
    """The whole seam: geometry needs an ATR and nothing else."""

    def __init__(self, atr=10.0):
        self._atr = atr

    def atr(self, timeframe, when, period):
        return self._atr


# --- the anti-drift contract ------------------------------------------------

def test_the_portal_and_the_generator_agree_on_what_is_valid():
    """A config the portal saves must be one the generator can run. If these two
    disagree, a strategy goes green on save and silently never fires."""
    assert validate_generator_config(RUN43) == []
    G.RulesSpec(RUN43)              # must not raise


def test_replay_imports_the_shared_brain_rather_than_reimplementing_it():
    """The one-way rule (CLAUDE.md): replay may import beacon_core, never the
    reverse. So the shared half lives here and replay adapts to it."""
    pytest.importorskip("harness.generators")
    from harness import generators as H
    assert H.RulesSpec is G.RulesSpec
    assert H.ConfigError is G.ConfigError
    assert H.DEFAULT_COOLDOWN_BARS == G.DEFAULT_COOLDOWN_BARS


# --- the decision, which both callers ask in the same order ------------------

def test_unknown_is_not_false():
    """Both sides UNKNOWN is counted and never emitted -- firing there would be
    trading on the absence of evidence."""
    spec = G.RulesSpec(RUN43)
    direction, why = G.decide_direction(spec, {"price": 100.0})   # no ta block
    assert direction is None and why == "n_unknown"


def test_both_sides_true_is_refused_not_guessed():
    cfg = dict(RUN43)
    cfg["long"] = {"when": {"type": "indicator", "id": "rsi", "timeframe": "15m",
                            "field": "value", "op": "gt", "value": 1}}
    cfg["short"] = dict(cfg["long"])
    spec = G.RulesSpec(cfg)
    ctx = {"price": 100.0, "ta": {"15m": {"rsi": {"value": 50}}}}
    direction, why = G.decide_direction(spec, ctx)
    assert direction is None and why == "n_both_sides_ambiguous"


# --- geometry: None propagates, it is never guessed --------------------------

def test_a_signal_is_dropped_when_its_stop_cannot_be_priced():
    """A generator that invents a stop is not testing the strategy it claims to."""
    spec = G.RulesSpec(RUN43)

    class _NoAtr:
        def atr(self, *a, **k):
            return None

    parsed, reason = G.build_signal(spec, "BUY", 100.0, _NoAtr(),
                                    dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc),
                                    {"price": 100.0})
    assert parsed is None and reason == "sl_unresolved"


def test_the_ladder_is_priced_off_the_stop_distance():
    spec = G.RulesSpec(RUN43)
    when = dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc)
    parsed, reason = G.build_signal(spec, "BUY", 100.0, _Provider(atr=10.0),
                                    when, {"price": 100.0})
    assert reason is None
    # sl = close - 1.5*ATR = 100 - 15 = 85, so risk = 15 and the rungs are
    # 1R/2R/3R above the entry.
    assert float(parsed.sl) == pytest.approx(85.0)
    assert [float(t) for t in parsed.tps] == pytest.approx([115.0, 130.0, 145.0])


def test_a_sell_prices_the_mirror_image():
    spec = G.RulesSpec(RUN43)
    when = dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc)
    parsed, _ = G.build_signal(spec, "SELL", 100.0, _Provider(atr=10.0), when,
                               {"price": 100.0})
    assert float(parsed.sl) == pytest.approx(115.0)
    assert [float(t) for t in parsed.tps] == pytest.approx([85.0, 70.0, 55.0])


# --- caps, held outside the loop so both callers suppress identically --------

def test_cooldown_suppresses_within_the_window_and_releases_after():
    spec = G.RulesSpec({**RUN43, "cooldown_bars": 10, "max_signals_per_day": 100})
    caps = G.CapState(spec)
    when = dt.datetime(2026, 8, 18, 9, tzinfo=dt.timezone.utc)
    assert caps.suppressed(100, when) is None
    caps.record(100, when)
    assert caps.suppressed(105, when) == "n_suppressed_cooldown"
    assert caps.suppressed(110, when) is None


def test_the_daily_cap_resets_on_the_next_day():
    spec = G.RulesSpec({**RUN43, "cooldown_bars": 0, "max_signals_per_day": 2})
    caps = G.CapState(spec)
    d1 = dt.datetime(2026, 8, 18, 9, tzinfo=dt.timezone.utc)
    for i in (1, 2):
        assert caps.suppressed(i, d1) is None
        caps.record(i, d1)
    assert caps.suppressed(3, d1) == "n_suppressed_max_per_day"
    assert caps.suppressed(4, d1 + dt.timedelta(days=1)) is None


def test_caps_default_to_something_rather_than_nothing():
    """A config that wants a signal on every bar has to say `cooldown_bars: 0`
    out loud -- that is a strategy decision, not a default to back into."""
    cfg = dict(RUN43)
    cfg.pop("cooldown_bars"); cfg.pop("max_signals_per_day")
    spec = G.RulesSpec(cfg)
    assert spec.cooldown_bars == G.DEFAULT_COOLDOWN_BARS > 0
    assert spec.max_per_day == G.DEFAULT_MAX_PER_DAY > 0
