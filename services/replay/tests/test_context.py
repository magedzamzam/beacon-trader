"""Entry-time context, and the NO-LOOK-AHEAD boundary.

This is the classic way a backtest lies to itself: an indicator computed on the
bar the signal arrived in has already seen the move it is being asked to
predict. The boundary lives in exactly one place (`ContextBuilder.closed_bars`),
so it is tested in exactly one place — hard.
"""
from __future__ import annotations

import datetime as dt

from harness import bars as B
from harness.context import ContextBuilder, filter_ctx
from conftest import T0, path_bars, series


def _hourly(n=60, base=4000.0):
    return B.BarSeries(path_bars(
        [(base + i, base + i + 4, base + i - 4, base + i + 1) for i in range(n)],
        start=T0, step_minutes=60))


def test_closed_bars_excludes_the_bucket_the_signal_lives_in():
    s = _hourly(10)
    cb = ContextBuilder(s)
    # 30 minutes into the 6th hourly bar: that bar has NOT closed yet.
    when = s[5].ts + dt.timedelta(minutes=30)
    win = cb.closed_bars("1h", when)
    assert [b.ts for b in win] == [s[i].ts for i in range(5)]
    assert s[5].ts not in [b.ts for b in win]


def test_a_bucket_that_closed_exactly_at_the_signal_instant_is_included():
    s = _hourly(10)
    cb = ContextBuilder(s)
    win = cb.closed_bars("1h", s[5].ts)        # bar 4 closed at bar 5's open
    assert win[-1].ts == s[4].ts


def test_closed_bars_respects_the_lookback_limit():
    s = _hourly(60)
    cb = ContextBuilder(s)
    assert len(cb.closed_bars("1h", s[50].ts, limit=10)) == 10


def test_atr_is_computed_from_strictly_prior_bars():
    s = _hourly(60)
    cb = ContextBuilder(s)
    when = s[40].ts
    from beacon_core.ta.indicators import atr as _atr
    win = cb.closed_bars("1h", when, 70)
    expected = _atr([b.high for b in win], [b.low for b in win],
                    [b.close for b in win], 14)
    assert cb.atr("1h", when) == expected


def test_atr_is_none_on_a_series_too_short_to_define_it():
    """Fail-open: the staged path then takes the executor's own no-ATR
    fallback, rather than the harness inventing a number."""
    assert ContextBuilder(_hourly(5)).atr("1h", T0 + dt.timedelta(hours=4)) is None


def test_the_candle_context_is_the_previous_completed_bar_not_the_signal_bar():
    """`build_plan` uses candle_high/low to decide MARKET vs LIMIT. Handing it
    the signal bar's own full range would let the planner fill on a touch that
    has not happened yet."""
    s = series([(4000, 4050, 3950, 4000), (4001, 4002, 4000, 4001)])
    mc = ContextBuilder(s).build(s[1].ts)
    assert mc.bar_index == 1
    assert float(mc.candle_high) == 4050.0        # bar 0
    assert float(mc.candle_low) == 3950.0
    assert float(mc.current_price) == 4001.0      # bar 1's OPEN mid


def test_the_first_bar_has_no_previous_candle_context():
    s = series([4000, 4001])
    mc = ContextBuilder(s).build(T0)
    assert mc.candle_high is None and mc.candle_low is None


def test_a_signal_after_the_last_bar_has_no_context():
    s = series([4000, 4001])
    assert ContextBuilder(s).build(T0 + dt.timedelta(days=5)) is None


def test_nothing_is_computed_when_no_rule_references_ta():
    s = _hourly(60)
    mc = ContextBuilder(s).build(s[40].ts, filter_rules=[])
    assert mc.ta == {} and mc.adx == {}
    assert mc.atr_1h is None                      # not requested


def test_the_ta_block_is_built_only_for_the_indicators_a_rule_names():
    s = _hourly(120)
    rules = [{"enabled": True, "mode": "shadow",
              "when": {"type": "indicator", "id": "rsi", "timeframe": "1h",
                       "field": "value", "op": "gt", "value": 50},
              "action": "skip"}]
    mc = ContextBuilder(s).build(s[100].ts, filter_rules=rules)
    assert "1h" in mc.ta
    keys = list(mc.ta["1h"])
    assert any(k.startswith("rsi") for k in keys)
    assert not any(k.startswith("ema") for k in keys)


def test_the_adx_block_is_built_for_an_adx_regime_rule():
    s = _hourly(200)
    rules = [{"enabled": True, "when": {"type": "adx_regime", "timeframe": "1h",
                                        "trending": True}, "action": "skip"}]
    mc = ContextBuilder(s).build(s[150].ts, filter_rules=rules)
    assert "1h" in mc.adx
    assert set(mc.adx["1h"]) == {"adx", "trending"}


def test_filter_ctx_has_the_keys_the_evaluator_reads():
    s = _hourly(200)
    rules = [{"enabled": True, "when": {"type": "adx_regime", "timeframe": "1h",
                                        "trending": True}, "action": "skip"}]
    mc = ContextBuilder(s).build(s[150].ts, filter_rules=rules)
    ctx = filter_ctx(mc, sessions=["london"])
    assert ctx["price"] and ctx["sessions"] == ["london"] and "adx" in ctx


def test_resampled_frames_are_built_once_and_reused():
    s = _hourly(60)
    cb = ContextBuilder(s)
    a, _ = cb._frame("1h")
    b, _ = cb._frame("1h")
    assert a is b
