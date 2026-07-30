"""Monte Carlo geometry null + Turtle Donchian breakout — the two shadow
strategies ported from PyPatel/Options-Trading-Strategies-in-Python. Pure."""
import math
import random

from beacon_core.analysis.montecarlo import (GetOneGaussianByBoxMuller,
                                             GetOneGaussianBySummation,
                                             SimpleMonteCarlo1, SimpleMonteCarloPut,
                                             barrier_outcomes, log_return_vol,
                                             signal_montecarlo)
from beacon_core.analysis.turtle import (reference_signals, signal_turtle,
                                         stateful_signals, strategy_returns)
from beacon_core.analysis.report import shadow_strategy_rollup
from beacon_core.execution import strategy as ST


# ============================ Monte Carlo =====================================
def _bs_call(S, K, T, r, v):
    d1 = (math.log(S / K) + (r + 0.5 * v * v) * T) / (v * math.sqrt(T))
    d2 = d1 - v * math.sqrt(T)
    N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    return S * N(d1) - K * math.exp(-r * T) * N(d2)


def test_reference_pricer_converges_to_black_scholes():
    """The ported SimpleMonteCarlo1 must price a European call correctly."""
    mc = SimpleMonteCarlo1(1.0, 100.0, 100.0, 0.20, 0.05, 100000, random.Random(7))
    assert abs(mc - _bs_call(100, 100, 1, 0.05, 0.20)) < 0.15


def test_reference_put_matches_put_call_parity():
    r, T = 0.05, 1.0
    call = SimpleMonteCarlo1(T, 100.0, 100.0, 0.20, r, 100000, random.Random(3))
    put = SimpleMonteCarloPut(T, 100.0, 100.0, 0.20, r, 100000, random.Random(3))
    # C - P = S - K*exp(-rT)
    assert abs((call - put) - (100.0 - 100.0 * math.exp(-r * T))) < 0.2


def test_both_gaussian_generators_are_standard_normal():
    for gen in (GetOneGaussianByBoxMuller, GetOneGaussianBySummation):
        rng = random.Random(11)
        v = [gen(rng) for _ in range(20000)]
        assert abs(sum(v) / len(v)) < 0.05
        assert abs((sum(x * x for x in v) / len(v)) ** 0.5 - 1.0) < 0.05


def test_win_rate_is_bought_with_payoff_ratio():
    """The reason this estimator exists: a high win-rate on lopsided geometry is
    arithmetic, not edge. A far stop with a near target wins ~90% of the time; the
    mirror geometry wins ~10%. Neither is skill."""
    kw = dict(direction="BUY", vol=0.15, expiry=24 / 6000, paths=6000, steps=48)
    wide = barrier_outcomes(spot=3300, sl=3270, tps=[3303], rng=random.Random(5), **kw)
    tight = barrier_outcomes(spot=3300, sl=3297, tps=[3330], rng=random.Random(5), **kw)
    assert wide["p_tp1_first"] > 0.85 and wide["rr_to_tp1"] == 0.1
    assert tight["p_tp1_first"] < 0.15 and tight["rr_to_tp1"] == 10.0


def test_null_breaks_even_on_every_geometry():
    """A driftless price process is a martingale: expected R is 0 whatever the
    stop/target layout. This is the calibration check on the whole simulation —
    a non-zero result means the sim is mis-specified, not that edge exists."""
    kw = dict(direction="BUY", vol=0.15, expiry=24 / 6000, paths=20000, steps=48)
    for sl, tp in ((3270, 3303), (3297, 3330), (3285, 3315)):
        b = barrier_outcomes(spot=3300, sl=sl, tps=[tp], rng=random.Random(21), **kw)
        assert abs(b["expected_r"]) < 0.05
        # ...and its closed-form twin: p_win must equal the break-even win-rate.
        assert abs(b["null_gap"]) < 0.02
        assert abs(b["breakeven_win_rate"] - 1.0 / (1.0 + b["rr_to_tp1"])) < 1e-4


def test_bridge_correction_removes_discretisation_bias():
    """Uncorrected discrete monitoring under-detects touches; the Brownian-bridge
    correction must recover the fine-grained answer from a coarse step count."""
    kw = dict(spot=3300, sl=3270, tps=[3303], direction="BUY", vol=0.15,
              expiry=24 / 6000, paths=6000)
    truth = barrier_outcomes(**kw, steps=1500, bridge=False, rng=random.Random(2))
    coarse = barrier_outcomes(**kw, steps=12, bridge=False, rng=random.Random(2))
    bridged = barrier_outcomes(**kw, steps=12, bridge=True, rng=random.Random(2))
    assert coarse["p_tp1_first"] < truth["p_tp1_first"] - 0.05      # biased low
    assert abs(bridged["p_tp1_first"] - truth["p_tp1_first"]) < 0.04


def test_short_horizon_is_flagged_and_withholds_the_null_gap():
    """When a big share of paths reach neither barrier the race never resolves:
    p_tp1_first understates the eventual rate, so the closed-form check would be a
    false alarm and is withheld. expected_r still holds — it prices the unresolved
    paths at market."""
    b = barrier_outcomes(spot=3300, sl=3200, tps=[3400], direction="BUY",
                         vol=0.10, expiry=1 / 6000, paths=3000, steps=8,
                         rng=random.Random(6))
    assert b["p_neither"] > 0.5
    assert b["horizon_truncated"] is True
    assert b["null_gap"] is None
    assert b["breakeven_win_rate"] is not None      # still reported for reference
    assert abs(b["expected_r"]) < 0.05


def test_resolved_race_reports_the_null_gap():
    b = barrier_outcomes(spot=3300, sl=3270, tps=[3303], direction="BUY",
                         vol=0.15, expiry=24 / 6000, paths=6000, steps=48,
                         rng=random.Random(6))
    assert b["horizon_truncated"] is False
    assert b["null_gap"] is not None and abs(b["null_gap"]) < 0.03


def test_rollup_counts_truncated_signals():
    rows = [{"channel": "c", "realized_pl": 1.0, "planned_risk": 1.0,
             "montecarlo": {"p_win_geometry": 0.5, "expected_r": 0.0,
                            "horizon_truncated": i < 3},
             "turtle": None} for i in range(5)]
    assert shadow_strategy_rollup(rows)["montecarlo"]["n_horizon_truncated"] == 3


def test_tp1_first_touch_equals_first_ladder_rung():
    """TP1 IS ladder[0] — the two fields must come from the same draws."""
    b = barrier_outcomes(spot=100, sl=98, tps=[101, 103, 106], direction="BUY",
                         vol=0.3, expiry=0.05, paths=3000, steps=20,
                         rng=random.Random(4))
    assert b["p_tp1_first"] == b["tp_ladder"][0]["p_reached_before_sl"]


def test_ladder_probabilities_are_monotonic():
    b = barrier_outcomes(spot=100, sl=98, tps=[101, 103, 106], direction="BUY",
                         vol=0.3, expiry=0.05, paths=3000, steps=20,
                         rng=random.Random(4))
    ps = [d["p_reached_before_sl"] for d in b["tp_ladder"]]
    assert ps == sorted(ps, reverse=True)


def test_sell_side_mirrors_buy_side():
    kw = dict(vol=0.15, expiry=24 / 6000, paths=4000, steps=24)
    buy = barrier_outcomes(spot=3300, sl=3270, tps=[3303], direction="BUY",
                           rng=random.Random(8), **kw)
    sell = barrier_outcomes(spot=3300, sl=3330, tps=[3297], direction="SELL",
                            rng=random.Random(8), **kw)
    assert abs(buy["p_tp1_first"] - sell["p_tp1_first"]) < 0.03


def test_malformed_geometry_returns_none():
    common = dict(vol=0.2, expiry=1.0, paths=10, steps=4)
    assert barrier_outcomes(spot=100, sl=110, tps=[120], direction="BUY", **common) is None
    assert barrier_outcomes(spot=100, sl=90, tps=[110], direction="SELL", **common) is None
    assert barrier_outcomes(spot=100, sl=100, tps=[110], direction="BUY", **common) is None
    assert barrier_outcomes(spot=100, sl=90, tps=[], direction="BUY", **common) is None


def test_signal_montecarlo_is_reproducible_and_seed_sensitive():
    kw = dict(entry=3300, sl=3270, tps=[3303, 3320], direction="BUY",
              closes=[3300 + ((i * 37) % 50) - 25 for i in range(120)], timeframe="1h")
    cfg = {"paths": 1500, "steps": 12, "price_paths": 1500}
    a = signal_montecarlo(**kw, cfg=cfg, seed=42)
    b = signal_montecarlo(**kw, cfg=cfg, seed=42)
    c = signal_montecarlo(**kw, cfg=cfg, seed=43)
    assert a["p_win_geometry"] == b["p_win_geometry"]
    assert a["p_win_geometry"] != c["p_win_geometry"]


def test_signal_montecarlo_needs_a_price_window():
    assert signal_montecarlo(entry=100, sl=98, tps=[102], direction="BUY",
                             closes=[100, 100], timeframe="1h") is None
    assert log_return_vol([100.0, 100.0, 100.0]) == 0.0


# ============================ Turtle ==========================================
_TREND = [100 + i * 0.5 for i in range(120)]                       # clean uptrend


def test_rolling_channel_excludes_the_current_bar():
    """`Close.shift(1).rolling(55)` — the window is the 55 bars BEFORE bar i."""
    ref = reference_signals(list(range(100)), 55)
    assert ref["high"][54] is None                     # not enough history yet
    assert ref["high"][55] == 54                       # max(closes[0:55])
    assert ref["low"][55] == 0
    assert ref["avg"][55] == sum(range(55)) / 55


def test_breakout_goes_long_in_an_uptrend():
    out = signal_turtle(closes=_TREND, direction="BUY", timeframe="1h")
    assert out["signal"] == 1.0
    assert out["position"] == "long"
    assert out["agrees"] is True
    assert out["long_entry"] is True


def test_agreement_flips_with_signal_direction():
    assert signal_turtle(closes=_TREND, direction="SELL", timeframe="1h")["agrees"] is False


def test_reference_signal_never_goes_flat():
    """The ported quirk: `Signal` is summed BEFORE the ffill, so a plain exit bar
    yields NaN and forward-fills. The reference stop-and-reverses; it is never 0."""
    wave = [100 + 10 * math.sin(i / 9.0) + i * 0.05 for i in range(220)]
    ref = reference_signals(wave, 55)
    seen = {v for v in ref["signal"] if v is not None}
    assert seen and 0.0 not in seen
    # ...whereas the documented-intent variant does go flat.
    assert 0 in set(stateful_signals(ref))


def test_flat_variant_exits_on_the_mean_cross():
    wave = [100 + 10 * math.sin(i / 9.0) for i in range(220)]
    ref = reference_signals(wave, 55)
    flat = stateful_signals(ref)
    for i in range(1, len(flat)):
        if flat[i - 1] == 1 and flat[i] == 0:
            assert ref["long_exit"][i]                 # only a mean cross flattens
            break


def test_strategy_returns_use_the_lagged_signal():
    closes = [100.0, 110.0, 121.0]
    # signal is +1 from the first bar -> both log returns count positively
    out = strategy_returns(closes, [1.0, 1.0, 1.0])
    assert out["n"] == 2
    assert abs(out["cum_log_return"] - 2 * math.log(1.1)) < 1e-6   # rounded to 6dp
    # a None (pre-history) signal contributes nothing
    assert strategy_returns(closes, [None, 1.0, 1.0])["n"] == 1


def test_turtle_needs_more_bars_than_the_window():
    assert signal_turtle(closes=_TREND[:40], direction="BUY", timeframe="1h") is None
    assert signal_turtle(closes=[], direction="BUY", timeframe="1h") is None


# ============================ filtration rules ================================
def _rule(when, action="skip", factor=0.5):
    return [{"enabled": True, "when": when, "action": action, "factor": factor}]


def test_mc_and_turtle_rules_are_inert_without_ctx():
    """Both ship fail-open: no ctx block -> no match (the #127 -> #132 pattern)."""
    for when in ({"type": "mc_probability", "max_expected_r": 0},
                 {"type": "turtle_signal", "agrees": False}):
        assert ST.apply_filter_rules(_rule(when), {}) == (1.0, False, [])


def test_mc_rule_matches_negative_expectancy_geometry():
    ctx = {"montecarlo": {"p_win_geometry": 0.88, "expected_r": -0.12, "rr_to_tp1": 0.1}}
    _, skip, _ = ST.apply_filter_rules(_rule({"type": "mc_probability", "max_expected_r": 0}), ctx)
    assert skip
    ctx_good = {"montecarlo": {"p_win_geometry": 0.11, "expected_r": 0.27, "rr_to_tp1": 10.0}}
    _, skip, _ = ST.apply_filter_rules(_rule({"type": "mc_probability", "max_expected_r": 0}), ctx_good)
    assert not skip


def test_mc_rule_requires_every_supplied_bound():
    ctx = {"montecarlo": {"p_win_geometry": 0.88, "expected_r": -0.12, "rr_to_tp1": 0.1}}
    when = {"type": "mc_probability", "max_expected_r": 0, "min_rr": 1.0}
    _, skip, _ = ST.apply_filter_rules(_rule(when), ctx)
    assert not skip                                    # rr 0.1 < 1.0 -> no match


def test_mc_rule_ignores_blank_ui_fields_and_never_raises():
    ctx = {"montecarlo": {"p_win_geometry": 0.88, "expected_r": -0.12, "rr_to_tp1": 0.1}}
    when = {"type": "mc_probability", "max_expected_r": 0, "min_rr": "", "max_p_win": None}
    _, skip, _ = ST.apply_filter_rules(_rule(when), ctx)
    assert skip
    # a rule with no usable bound at all is a no-op, not a match-all
    assert ST.apply_filter_rules(_rule({"type": "mc_probability", "min_rr": ""}), ctx) == (1.0, False, [])


def test_turtle_rule_matches_disagreement_and_position():
    ctx = {"turtle": {"agrees": False, "position": "short", "position_flat": "flat"}}
    _, skip, _ = ST.apply_filter_rules(_rule({"type": "turtle_signal", "agrees": False}), ctx)
    assert skip
    when = {"type": "turtle_signal", "position": "flat", "variant": "signal_flat"}
    _, skip, _ = ST.apply_filter_rules(_rule(when), ctx)
    assert skip                                        # reads position_flat, not position
    when_ref = {"type": "turtle_signal", "position": "flat"}
    _, skip, _ = ST.apply_filter_rules(_rule(when_ref), ctx)
    assert not skip                                    # reference variant is "short"


def test_turtle_rule_scales_instead_of_skipping():
    ctx = {"turtle": {"agrees": False, "position": "short"}}
    factor, skip, _ = ST.apply_filter_rules(
        _rule({"type": "turtle_signal", "agrees": False}, action="scale", factor=0.5), ctx)
    assert (factor, skip) == (0.5, False)


def test_shadow_rule_inputs_reports_what_needs_plumbing():
    rules = _rule({"type": "mc_probability", "max_expected_r": 0}) + \
        _rule({"type": "turtle_signal", "agrees": False}) + \
        _rule({"type": "session_in", "sessions": ["LONDON"]})
    assert ST.shadow_rule_inputs(rules) == {"montecarlo", "turtle"}
    assert ST.shadow_rule_inputs(_rule({"type": "session_in", "sessions": []})) == set()


# ============================ report rollup ===================================
def _row(channel, pl, p_geo, agrees, risk=10.0, er=0.0):
    return {"channel": channel, "realized_pl": pl, "planned_risk": risk,
            "montecarlo": {"p_win_geometry": p_geo, "expected_r": er},
            "turtle": {"agrees": agrees}}


def _channel(name, n, win_rate, p_geo):
    wins = int(round(n * win_rate))
    return [_row(name, 1.0 if i < wins else -1.0, p_geo, True) for i in range(n)]


def test_rollup_separates_geometry_from_skill():
    """Two channels, identical 80% win-rates. 'lucky' posts geometry worth 80%
    (its win-rate is arithmetic); 'good' posts geometry worth 40% (real edge)."""
    out = shadow_strategy_rollup(_channel("lucky", 60, 0.8, 0.8)
                                 + _channel("good", 60, 0.8, 0.4),
                                 significance_n=30)
    lucky = out["montecarlo"]["by_channel"]["lucky"]
    good = out["montecarlo"]["by_channel"]["good"]
    assert lucky["actual_win_rate"] == good["actual_win_rate"] == 0.8
    assert abs(lucky["edge"]) < 1e-9          # identical win-rate, zero edge
    assert good["edge"] == 0.4
    assert good["beats_null"] and good["significant"]
    assert not lucky["beats_null"]


def test_rollup_will_not_call_an_edge_on_a_thin_sample():
    """The same 40pp edge at n=10 must NOT clear the null — the Beta-Binomial
    posterior shrinks a thin sample toward its own null (CLAUDE.md §4)."""
    out = shadow_strategy_rollup(_channel("good", 10, 0.8, 0.4), significance_n=30)
    good = out["montecarlo"]["by_channel"]["good"]
    assert good["edge"] == 0.4                # the point estimate still looks big
    assert not good["beats_null"]             # ...but the interval does not clear it
    assert not good["significant"]


def test_rollup_reports_r_against_the_null():
    rows = [_row("c", 20.0, 0.5, True, risk=10.0, er=-0.5) for _ in range(4)]
    mc = shadow_strategy_rollup(rows)["montecarlo"]
    assert mc["actual_mean_r"] == 2.0          # +20 on 10 risk
    assert mc["null_mean_r"] == -0.5
    assert mc["r_edge"] == 2.5


def test_rollup_splits_turtle_agreement_and_counts_unknowns():
    rows = [_row("c", 1.0, 0.5, True), _row("c", -1.0, 0.5, False),
            _row("c", 1.0, 0.5, None)]
    tu = shadow_strategy_rollup(rows)["turtle"]
    assert tu["overall"]["agrees"]["n"] == 1
    assert tu["overall"]["disagrees"]["n"] == 1
    assert tu["n_unknown"] == 1


def test_rollup_skips_unlabelled_and_blockless_rows():
    rows = [_row("c", None, 0.5, True),                       # no outcome
            {"channel": "c", "realized_pl": 1.0, "planned_risk": 1.0,
             "montecarlo": None, "turtle": None}]             # estimators degraded
    out = shadow_strategy_rollup(rows)
    assert out["n_labelled"] == 0
    assert out["montecarlo"]["by_channel"] == {}
    assert out["turtle"]["overall"] == {}
