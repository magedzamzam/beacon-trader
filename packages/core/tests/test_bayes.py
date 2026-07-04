"""Unit tests for the Bayesian correlation engine (pure, no DB)."""
import random

from beacon_core.analysis import bayes as B


def test_betainc_known_values():
    assert abs(B.betainc(1, 1, 0.37) - 0.37) < 1e-6      # I_x(1,1) == x
    assert abs(B.betainc(2, 2, 0.5) - 0.5) < 1e-6        # symmetric
    assert abs(B.beta_ppf(0.5, 1, 1) - 0.5) < 1e-3


def test_posterior_shrinks_small_samples():
    p = B.posterior(2, 2, 0.5, 20.0, 0.90)               # 2/2 wins, base 50%
    assert p["mean"] < 0.65                              # not 100%
    assert p["ci_high"] < 1.0 and p["ci_low"] < p["mean"] < p["ci_high"]


def test_posterior_moves_with_strong_evidence():
    p = B.posterior(50, 60, 0.5, 20.0, 0.90)
    assert p["mean"] > 0.6 and p["ci_low"] > 0.5


def _dataset(n=120, seed=5, strength=0.85):
    random.seed(seed)
    ex = []
    for i in range(n):
        high = i % 2 == 0
        feats = {"1h": {"rsi_14": {"value": 80.0 if high else 20.0},
                        "ema_50": {"value": 100, "above": high}}}
        win = high if random.random() < strength else (not high)
        ex.append((feats, win))
    return ex


def test_build_model_and_conditions():
    m = B.build_model(_dataset(), min_n=5)
    assert m["ready"] and m["n"] == 120 and m["conditions"]
    # the predictive ema flag should surface with a non-trivial lift
    assert any(abs(c["lift"]) > 0.1 for c in m["conditions"])


def test_score_brackets_base_rate():
    m = B.build_model(_dataset(), min_n=5)
    hi = B.score(m, {"1h": {"rsi_14": {"value": 82.0}, "ema_50": {"value": 100, "above": True}}})
    lo = B.score(m, {"1h": {"rsi_14": {"value": 18.0}, "ema_50": {"value": 100, "above": False}}})
    assert hi["p_win"] > m["base_rate"] > lo["p_win"]
    assert 0.0 < lo["p_win"] < 1.0 and 0.0 < hi["p_win"] < 1.0


def test_empty_and_cold_start():
    assert B.build_model([])["ready"] is False
    m = B.build_model([({"1h": {"rsi_14": {"value": 50}}}, True)], min_n=5)
    # one example: model builds but nothing meets min_n
    assert m["ready"] and m["conditions"] == []
