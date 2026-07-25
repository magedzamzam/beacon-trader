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
        self.sl = self.entry_from = self.entry_to = None      # #128 geometry (optional)
        self.tps = []


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


def test_adx_by_tf_reads_trending_and_value():
    # #127: per-TF ADX pulled from the persisted adx_14-style block (prefix-tolerant)
    feats = {"4h": {"adx_14": {"adx": 31.2, "trending": True}},
             "1h": {"adx": {"adx": 18.0, "trending": False}},
             "1d": {"ema_200": {"value": 4100}}}          # no ADX -> omitted
    m = E.adx_by_tf(feats)
    assert m["4h"] == {"adx": 31.2, "trending": True}
    assert m["1h"] == {"adx": 18.0, "trending": False}
    assert "1d" not in m
    assert E.adx_by_tf({}) == {}


def test_adx_regime_shadow_would_skip():
    # trending on 4h -> the shadow rule would skip; ranging -> would not.
    trend = E.adx_regime_shadow(_Ctx([], {"4h": {"adx_14": {"adx": 34.0, "trending": True}}}))
    assert trend["trending"] is True and trend["would_skip"] is True and trend["primary_adx"] == 34.0
    rng = E.adx_regime_shadow(_Ctx([], {"4h": {"adx_14": {"adx": 12.0, "trending": False}}}))
    assert rng["trending"] is False and rng["would_skip"] is False
    # no ADX anywhere -> None (nothing to measure), never gates
    assert E.adx_regime_shadow(_Ctx([], {})) is None


def test_sl_geometry_atr_units():
    # #128: BUY entry 4180, SL 4160 (20), TP1 4210 (30), ATR 10 (abs price units)
    g = E.sl_geometry(4180, 4160, [4210, 4240], atr=10.0)
    assert g["sl_dist_atr"] == 2.0                   # 20 / 10
    assert g["tp1_dist_atr"] == 3.0                  # 30 / 10
    assert g["rr_to_tp1"] == 1.5                     # 30 / 20
    assert g["sl_inside_1_atr"] is False             # 20 > 10
    # a tight stop inside one expected move
    g2 = E.sl_geometry(100, 95, [110], atr=8.0)
    assert g2["sl_inside_1_atr"] is True             # 5 <= 8
    # entry band (range entry) measured in ATR
    g3 = E.sl_geometry(100, 90, [120], atr=5.0, entry_from=100, entry_to=102.5)
    assert g3["entry_band_atr"] == 0.5               # 2.5 / 5
    # unusable inputs -> None (never gates)
    assert E.sl_geometry(100, None, [110], atr=10.0) is None
    assert E.sl_geometry(100, 95, [110], atr=0.0) is None


def test_sl_geometry_estimator_reads_ctx():
    ctx = _Ctx([], {"1h": {"atr_14": {"value": 10.0, "pct": 0.24}}}, price=4180.0)
    ctx.sl, ctx.entry_from, ctx.entry_to, ctx.tps = 4160.0, 4180.0, 4180.0, [4210.0]
    g = E.sl_geometry_estimator(ctx)
    assert g["sl_dist_atr"] == 2.0 and g["rr_to_tp1"] == 1.5
    # no ATR in features -> None
    bare = _Ctx([], {}, price=4180.0)
    bare.sl, bare.entry_from, bare.tps = 4160.0, 4180.0, [4210.0]
    assert E.sl_geometry_estimator(bare) is None


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok ", n)
    print("ALL PASS")
