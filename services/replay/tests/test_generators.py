"""`generator:rules` — a strategy expressed as JSON (#184).

The acceptance criteria, as tests:

  * a NEW strategy is a new config, with no code change and no redeploy — two
    different indicator combinations both produce signals from this one module;
  * the condition grammar is the SHARED one, so `all`/`any`/`not` over any
    registry indicator works here exactly as it does in `entry_filters`;
  * FAIL-OPEN: a condition referencing an unknown indicator, an absent field or
    a series too thin emits NOTHING and is counted — never fires on missing data;
  * `entry`/`sl`/`tps` produce a `ParsedSignal` that passes
    `planner.validate_signal`; an unpriceable or invalid geometry is dropped and
    reported, not silently completed with a guessed level;
  * `cooldown_bars` and `max_signals_per_day` bind and report what they
    suppressed — a condition true for 50 bars would otherwise emit 50 signals
    and the RISK CAPS would become the strategy;
  * NO LOOK-AHEAD: every bar a condition saw had closed by the signal's own
    instant, asserted against the frame rather than trusted to a comment;
  * a generated signal reaches the same simulator, unchanged.

Pure and synthetic, like the rest of this suite.
"""
from __future__ import annotations

import datetime as dt

import pytest

from harness import bars as B
from harness import generators as G
from harness import signal_sources as SS
from harness.context import ContextBuilder
from harness.portfolio import PortfolioSim, SignalRow
from harness.variants import build_variant
from conftest import NO_RATCHET, T0, path_bars, series, variant_dict

ALWAYS = {"type": "always"}
RSI_UP = {"type": "indicator", "id": "rsi", "timeframe": "15m",
          "field": "value", "op": "gt", "value": 50}
RSI_DOWN = {"type": "indicator", "id": "rsi", "timeframe": "15m",
            "field": "value", "op": "lt", "value": 50}
UNKNOWN_INDICATOR = {"type": "indicator", "id": "not_an_indicator",
                     "timeframe": "15m", "field": "value", "op": "gt", "value": 1}

ATR_SL = {"type": "atr_mult", "timeframe": "15m", "period": 14, "mult": 1.5}
R_LADDER = [{"type": "r_mult", "r": 1.0}, {"type": "r_mult", "r": 2.0}]


def zigzag(n_minutes: int = 5000, *, amplitude: float = 6.0,
           period_minutes: int = 240, base: float = 4000.0):
    """A deterministic, moving 1m series.

    Moving matters: a flat series has ATR 0, every geometry collapses to zero
    risk, and a test would pass by dropping every signal for the wrong reason."""
    mids = []
    for i in range(n_minutes):
        phase = (i % period_minutes) / period_minutes
        # triangle wave — continuous, no RNG, and identical on every machine
        mids.append(base + amplitude * (2 * phase if phase < 0.5 else 2 * (1 - phase)))
    return B.BarSeries(path_bars(mids))


def cfg(**kw) -> dict:
    base = {"timeframe": "15m", "long": {"when": ALWAYS}, "entry": {"type": "close"},
            "sl": ATR_SL, "tps": list(R_LADDER), "cooldown_bars": 0,
            "max_signals_per_day": 0}
    base.update(kw)
    return base


def run(config: dict, s: B.BarSeries = None):
    s = s or zigzag()
    return G.rules_generator(s.bars, config)


# --- registration --------------------------------------------------------------
def test_the_one_generator_that_ships_is_the_config_driven_one():
    """Not `generator:macd` + `generator:fvg` + ... — registering one Python
    function per idea is exactly #167's mistake repeated on the generation side."""
    assert SS.available_generators() == [G.NAME] == ["rules"]
    assert SS.is_generator("generator:rules") is True


def test_it_runs_through_the_seam_like_any_other_generator():
    out = SS.run_generator("generator:rules", zigzag().bars, cfg())
    assert out and all(isinstance(g, SS.GeneratedSignal) for g in out)
    assert out.stats["n_emitted"] == len(out)


# --- a strategy is JSON, not a deploy ------------------------------------------
def test_a_novel_indicator_combination_needs_no_code_change():
    """Two different strategies, one module, zero edits between them."""
    a = run(cfg(long={"when": {"all": [RSI_UP, {"type": "indicator", "id": "ema",
                                                "timeframe": "15m", "field": "value",
                                                "op": "lt", "params": {"period": 20},
                                                "ref": "price"}]}}))
    b = run(cfg(long={"when": {"any": [RSI_UP, {"type": "indicator", "id": "macd",
                                                "timeframe": "15m", "field": "macd",
                                                "op": "gt", "value": 0}]}}))
    assert a.stats["n_emitted"] > 0
    assert b.stats["n_emitted"] > 0
    assert a.stats["n_emitted"] != b.stats["n_emitted"]      # different strategies


def test_long_and_short_are_independent_expressions():
    out = run(cfg(long={"when": RSI_UP}, short={"when": RSI_DOWN}))
    assert out.stats.get("n_emitted_BUY", 0) > 0
    assert out.stats.get("n_emitted_SELL", 0) > 0


def test_a_bar_where_both_sides_hold_emits_neither():
    """`always` on both sides is the degenerate case of a contradictory config.
    Emitting one arbitrarily would make the result depend on evaluation order."""
    out = run(cfg(long={"when": ALWAYS}, short={"when": ALWAYS}))
    assert out.stats["n_emitted"] == 0
    assert out.stats["n_both_sides_ambiguous"] == out.stats["n_bars_evaluated"]


# --- fail-open ------------------------------------------------------------------
def test_an_unknown_indicator_emits_nothing_and_is_counted():
    out = run(cfg(long={"when": UNKNOWN_INDICATOR}))
    assert out.stats["n_emitted"] == 0
    assert out.stats["n_unknown"] == out.stats["n_bars_evaluated"]


def test_not_over_a_missing_input_does_not_manufacture_a_signal():
    """The failure the three-valued grammar exists to prevent: a generator that
    fires BECAUSE it could not compute the indicator would be trading on the
    absence of evidence."""
    out = run(cfg(long={"when": {"not": UNKNOWN_INDICATOR}}))
    assert out.stats["n_emitted"] == 0


def test_a_series_too_thin_for_its_own_indicators_emits_nothing():
    out = run(cfg(long={"when": RSI_UP}), series([4000.0] * 200))
    assert out.stats["n_emitted"] == 0


# --- geometry --------------------------------------------------------------------
def test_every_emitted_signal_passes_the_real_geometry_gate():
    from beacon_core.execution.planner import validate_signal
    out = run(cfg(long={"when": RSI_UP}, short={"when": RSI_DOWN}))
    assert out
    for g in out:
        ok, why = validate_signal(g.parsed)
        assert ok, why


def test_the_ladder_is_ordered_outward_from_entry():
    out = run(cfg(long={"when": RSI_UP},
                  tps=[{"type": "r_mult", "r": 3.0}, {"type": "r_mult", "r": 1.0},
                       {"type": "r_mult", "r": 2.0}]))
    g = out[0]
    e = float(g.parsed.entry_to)
    tps = [float(t) for t in g.parsed.tps]
    assert tps == sorted(tps)                      # BUY: outward = ascending
    assert all(t > e for t in tps)


def test_r_multiples_are_measured_against_the_generators_own_stop():
    out = run(cfg(long={"when": RSI_UP}, tps=[{"type": "r_mult", "r": 2.0}]))
    g = out[0]
    e, sl, tp = (float(g.parsed.entry_to), float(g.parsed.sl), float(g.parsed.tps[0]))
    assert tp - e == pytest.approx(2 * (e - sl), rel=1e-3)


def test_a_points_stop_is_exactly_that_many_points():
    out = run(cfg(long={"when": RSI_UP}, sl={"type": "points", "points": 7.5}))
    g = out[0]
    assert float(g.parsed.entry_to) - float(g.parsed.sl) == pytest.approx(7.5, abs=1e-4)


def test_an_unpriceable_stop_drops_the_signal_and_says_why():
    """A generator that invented a stop would not be testing the strategy it
    claims to."""
    out = run(cfg(long={"when": ALWAYS},
                  sl={"type": "level", "id": "not_an_indicator",
                      "timeframe": "15m", "field": "bottom"}))
    assert out.stats["n_emitted"] == 0
    assert out.stats["n_dropped_geometry"] == out.stats["n_triggered"]
    assert out.stats["dropped_geometry_breakdown"]["sl_unresolved"] > 0


def test_a_geometry_the_planner_would_reject_is_dropped_not_emitted():
    """A TP on the wrong side of entry. `donchian.lower` sits below the close, so
    a BUY taking profit there is a target behind the entry."""
    out = run(cfg(long={"when": ALWAYS},
                  tps=[{"type": "level", "id": "donchian", "timeframe": "15m",
                        "field": "lower"}]))
    assert out.stats["n_emitted"] == 0
    reasons = out.stats["dropped_geometry_breakdown"]
    assert any(k.startswith("invalid_geometry") for k in reasons), reasons


def test_a_zero_width_stop_is_a_drop_not_a_division_by_zero():
    out = run(cfg(long={"when": ALWAYS}, sl={"type": "points", "points": 0}))
    assert out.stats["n_emitted"] == 0
    assert out.stats["dropped_geometry_breakdown"]["sl_unresolved"] > 0


# --- the caps that stop the risk limits becoming the strategy --------------------
def test_cooldown_bars_binds_and_reports_what_it_suppressed():
    hot = run(cfg(long={"when": ALWAYS}, cooldown_bars=0))
    cool = run(cfg(long={"when": ALWAYS}, cooldown_bars=10))
    assert hot.stats["n_emitted"] > cool.stats["n_emitted"]
    assert cool.stats["n_suppressed_cooldown"] > 0
    # ...and the spacing is real, not just the count.
    ts = [g.at for g in cool]
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(ts, ts[1:])]
    assert all(gap >= 10 * 15 for gap in gaps)


def test_max_signals_per_day_binds_and_reports_what_it_suppressed():
    out = run(cfg(long={"when": ALWAYS}, max_signals_per_day=2))
    assert out.stats["n_suppressed_max_per_day"] > 0
    per_day: dict = {}
    for g in out:
        per_day[g.at.date()] = per_day.get(g.at.date(), 0) + 1
    assert max(per_day.values()) <= 2


def test_the_caps_default_to_something_rather_than_nothing():
    """A condition true for 50 consecutive bars emits 50 signals, each opening a
    position, and `max_open_risk_per_symbol` then decides the strategy. Both caps
    are non-zero by default so a config has to ASK for that."""
    assert G.DEFAULT_COOLDOWN_BARS > 0 and G.DEFAULT_MAX_PER_DAY > 0
    out = run({"timeframe": "15m", "long": {"when": ALWAYS},
               "sl": ATR_SL, "tps": list(R_LADDER)})
    assert out.stats["cooldown_bars"] == G.DEFAULT_COOLDOWN_BARS
    assert out.stats["max_signals_per_day"] == G.DEFAULT_MAX_PER_DAY


# --- no look-ahead ----------------------------------------------------------------
def test_a_generated_signal_saw_only_bars_that_had_already_closed(monkeypatch):
    """The classic backtest fraud, checked against the frame. Every bucket handed
    to an indicator must have CLOSED at or before the signal's own instant —
    mirrors `test_context.py`'s boundary assertion for the historical source."""
    seen = []
    original = ContextBuilder.closed_bars

    def spy(self, timeframe, before, limit=None):
        win = (original(self, timeframe, before) if limit is None
               else original(self, timeframe, before, limit))
        seen.append((timeframe, before, win))
        return win

    monkeypatch.setattr(ContextBuilder, "closed_bars", spy)
    out = run(cfg(long={"when": RSI_UP}))
    assert out and seen

    emitted = {g.at for g in out}
    checked_at_an_emission = False
    for timeframe, before, win in seen:
        minutes = B.timeframe_minutes(timeframe) or 1
        # Every bucket handed to an indicator had CLOSED by the instant it was
        # asked about — nothing still in progress, ever.
        for b in win:
            assert b.ts + dt.timedelta(minutes=minutes) <= before
        checked_at_an_emission = checked_at_an_emission or before in emitted
    # ...and at least one of those instants IS a signal's own timestamp, so the
    # assertion above actually covers the emissions and not only the quiet bars.
    assert checked_at_an_emission


def test_a_signal_is_stamped_at_the_close_of_its_trigger_bucket():
    out = run(cfg(long={"when": RSI_UP}))
    frame_closes = {b.ts + dt.timedelta(minutes=15)
                    for b in B.resample(zigzag().bars, "15m")}
    assert all(g.at in frame_closes for g in out)


# --- nothing downstream changes ----------------------------------------------------
def test_a_generated_signal_reaches_the_same_simulator_unchanged():
    """No parallel execution stack: a generated signal is planned, sized,
    filtered, capped and scored by exactly the code a Telegram signal is."""
    s = zigzag()
    out = run(cfg(long={"when": RSI_UP}, cooldown_bars=40), s)
    rows = [SignalRow(id=-(i + 1), at=g.at, parsed=g.parsed, source_id=None,
                      source_name="generator", account_ids=(1,))
            for i, g in enumerate(out)]
    assert rows
    v = build_variant(variant_dict(sl_rules=NO_RATCHET))
    res = PortfolioSim(v, s).run(rows)
    assert res.counts["taken"] > 0
    assert any(t.ever_filled for t in res.trades)


def test_a_generated_run_reports_through_the_same_path_as_a_historical_one():
    """No separate reporting path (#184): the same `variant_report`, so the same
    `geometry_ab_rollup` keys AND the same guardrail block. A generated result
    that reported its own way would be uncomparable to a live arm — and would be
    the obvious place for the overfitting guardrails to quietly go missing."""
    from harness import metrics as M
    s = zigzag()
    out = run(cfg(long={"when": RSI_UP}, cooldown_bars=40), s)
    rows = [SignalRow(id=-(i + 1), at=g.at, parsed=g.parsed, source_id=None,
                      source_name="generator", account_ids=(1,))
            for i, g in enumerate(out)]
    v = build_variant(variant_dict(sl_rules=NO_RATCHET))
    res = PortfolioSim(v, s).run(rows)
    holdout = rows[len(rows) // 2].at
    rep = M.variant_report(res, variant=v, series=s, holdout_from=holdout,
                           n_variants_searched=12)

    assert rep["headline_basis"] == "held_out"          # the only reportable one
    assert rep["guardrails"]["n_variants_searched"] == 12
    assert rep["guardrails"]["best_of_n_inflation_sigma"] is not None
    assert "regime" in rep["guardrails"]                # window composition
    assert "verdict_withheld" in rep and "n_closed" in rep
    for k in ("n_never_filled", "n_horizon_capped", "n_same_bar_ambiguous_legs"):
        assert k in rep["caveats"]
    assert {"n_closed", "base_rate", "by_arm"} <= set(rep["headline"])


def test_the_stats_ride_on_the_run_header_not_in_a_log_line():
    from harness import runner as R
    s = zigzag()
    out = run(cfg(long={"when": RSI_UP}), s)
    spec = R.RunSpec(signal_source="generator:rules", variants=[])
    head = R.sweep(spec, s, [], generator_stats=out.stats)["run"]
    assert head["generator"]["n_emitted"] == out.stats["n_emitted"]
    assert "not_a_route_to_live" in head["generator"]


def test_the_stats_restate_that_this_is_not_a_route_to_live():
    """A backtest result is a screening step. The Lever-5 chain has to be stated
    where the number is, or it will be forgotten by the person reading it."""
    text = run(cfg()).stats["not_a_route_to_live"]
    assert "engine" in text and "shadow forward-R" in text


# --- config validation is loud, not silent ------------------------------------------
def test_a_generator_without_a_stop_is_a_config_error():
    """Silence would be worse: a generator that produced nothing is
    indistinguishable from a strategy that never triggered, and a flat equity
    curve reads as a finding."""
    with pytest.raises(G.ConfigError) as exc:
        G.RulesSpec({"timeframe": "15m", "long": {"when": ALWAYS},
                     "tps": list(R_LADDER)})
    assert "SL" in str(exc.value)


def test_a_generator_without_a_tp_ladder_is_a_config_error():
    with pytest.raises(G.ConfigError):
        G.RulesSpec({"timeframe": "15m", "long": {"when": ALWAYS}, "sl": ATR_SL})


def test_a_generator_with_no_condition_at_all_is_a_config_error():
    with pytest.raises(G.ConfigError):
        G.RulesSpec({"timeframe": "15m", "sl": ATR_SL, "tps": list(R_LADDER)})


def test_an_unknown_geometry_type_is_a_config_error():
    with pytest.raises(G.ConfigError):
        G.RulesSpec(cfg(sl={"type": "vibes"}))
    with pytest.raises(G.ConfigError):
        G.RulesSpec(cfg(tps=[{"type": "vibes"}]))


def test_an_unknown_timeframe_is_a_config_error():
    with pytest.raises(G.ConfigError):
        G.RulesSpec(cfg(timeframe="13m"))


def test_the_config_declares_the_indicator_instances_it_needs():
    """What `check --config` prints, so an unknown id surfaces as a missing
    requirement instead of a strategy that mysteriously never fires."""
    from beacon_core.execution import strategy as ST
    spec = G.RulesSpec(cfg(long={"when": {"all": [RSI_UP, UNKNOWN_INDICATOR]}}))
    ids = {r["id"] for r in ST.condition_requirements(spec.long, spec.timeframe)}
    assert ids == {"rsi"}                          # the unknown one simply is not there
