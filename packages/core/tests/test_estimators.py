"""Phase-1 shadow estimators (#53): Hurst, Kalman slope, realized vol, regime,
VWAP z. Pure math — runs on a bare box (k-NN/report need the DB, tested in CI)."""
from beacon_core.analysis import estimators as E


def _ramp(n=64, step=0.5, start=100.0):
    return [start + i * step for i in range(n)]


def test_hurst_separates_trending_from_mean_reverting():
    assert E.hurst_rs(_ramp()) > 0.6                 # persistent / trending
    altern = [100 + (2 if i % 2 else -2) for i in range(64)]
    assert E.hurst_rs(altern) < 0.5                  # anti-persistent
    assert E.hurst_rs([1, 2, 3]) is None             # too few points


def test_kalman_recovers_constant_velocity():
    k = E.kalman_slope(_ramp(step=0.5))
    assert k["method"] == "kalman_cv"
    assert abs(k["slope"] - 0.5) < 0.05              # tracks the true ramp slope
    assert E.kalman_slope([1, 2]) is None


def test_kalman_slope_sign_follows_trend():
    assert E.kalman_slope(_ramp(step=-0.5))["slope"] < 0
    assert E.kalman_slope(_ramp(step=0.5))["slope"] > 0


def test_realized_vol():
    assert E.realized_vol([100.0] * 10) == 0.0       # flat -> zero vol
    assert E.realized_vol(_ramp()) is not None
    assert E.realized_vol([100.0]) is None


def test_classify_regime_priority():
    # volatility spike dominates even when ADX says trending
    assert E.classify_regime(40, 0.2, 1.5, 0.7) == "high_vol"
    assert E.classify_regime(30, 0.2, 0.1, 0.4) == "trending"    # ADX >= 25
    assert E.classify_regime(10, 0.2, 0.1, 0.7) == "trending"    # Hurst > 0.55
    assert E.classify_regime(10, 0.2, 0.1, 0.4) == "ranging"


def test_vwap_z_signed_and_scaled():
    d = E.vwap_z(101.0, 100.0, _ramp())
    assert d["deviation"] == 1.0 and d["deviation_pct"] == 1.0 and d["z"] > 0
    assert E.vwap_z(99.0, 100.0, _ramp())["deviation"] < 0        # below VWAP
    assert E.vwap_z(None, 100.0, _ramp()) is None                 # missing price


class _Ctx:
    def __init__(self, closes, features, price=101.0, tf="1h"):
        self.closes, self.features, self.price, self.timeframe = closes, features, price, tf
        self.session = None


def test_ctx_estimators_read_features():
    ctx = _Ctx(_ramp(), {"1h": {"adx": {"adx": 30}, "atr": {"pct": 0.2},
                                 "vwap": {"value": 100.0}}})
    assert E.regime(ctx)["label"] == "trending"
    assert E.hurst(ctx)["value"] > 0.6
    assert E.kalman(ctx)["slope"] > 0
    assert E.vwap_deviation(ctx)["z"] is not None


def test_ctx_estimators_degrade_gracefully_on_missing_data():
    ctx = _Ctx([], {})                               # no window, no features
    # regime still returns a label (ranging) from all-None inputs; series ones skip
    assert E.regime(ctx)["label"] == "ranging"
    assert E.hurst(ctx) is None
    assert E.kalman(ctx) is None
    assert E.vwap_deviation(ctx) is None


def test_regime_reads_param_suffixed_feature_keys():
    # Persisted blocks use param-suffixed keys (adx_14/atr_14), not bare adx/atr
    # (#111). The estimator must surface real adx/atr_pct via prefix match.
    feats = {"1h": {"adx_14": {"adx": 30.0, "trending": True},
                    "atr_14": {"value": 12.2, "pct": 0.29}}}
    r = E.regime(_Ctx([], feats))          # empty window -> no rvol/hurst noise
    assert r["adx"] == 30.0
    assert r["atr_pct"] == 0.29
    assert r["label"] == "trending"        # ADX 30 >= 25


def test_regime_yields_more_than_one_label_across_mixed_inputs():
    # A range signal (low ADX, no window so hurst/rvol are None) must NOT collapse
    # to "trending" the way the pre-#111 dead-key path did.
    ranging = E.regime(_Ctx([], {"1h": {"adx_14": {"adx": 12.0},
                                        "atr_14": {"pct": 0.15}}}))
    trending = E.regime(_Ctx([], {"1h": {"adx_14": {"adx": 34.0},
                                         "atr_14": {"pct": 0.21}}}))
    assert ranging["label"] == "ranging"
    assert trending["label"] == "trending"
    assert len({ranging["label"], trending["label"]}) > 1


def test_regime_backward_compatible_with_bare_keys():
    # bare (unsuffixed) keys still resolve, so older synthetic blocks keep working
    r = E.regime(_Ctx([], {"1h": {"adx": {"adx": 40.0}, "atr": {"pct": 0.3}}}))
    assert r["adx"] == 40.0 and r["atr_pct"] == 0.3


def test_regime_adx_feeds_knn_vector():
    # #111 latent path: once regime() writes real adx/atr_pct, the k-NN feature
    # vector picks them up (previously two dead dimensions).
    r = E.regime(_Ctx([], {"1h": {"adx_14": {"adx": 27.0}, "atr_14": {"pct": 0.4}}}))
    vec = E._feature_vector({"regime": r})
    assert vec is not None
    assert vec[0] == 27.0 and vec[1] == 0.4


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok ", n)
    print("ALL PASS")
