"""Unified per-signal feature vector (#62): assemble TA + analytics + structure/
magnet + AI + session into one namespaced dict and prove the Bayesian model now
sees more than TA. Pure — no DB (the async loader is exercised in CI)."""
from beacon_core.analysis import feature_vector as FV
from beacon_core.analysis import bayes as B


def _full():
    return FV.assemble(
        ta_features={"1h": {"rsi_14": {"value": 55.0},
                            "ema_200": {"value": 100.0, "above": True}}},
        session_tag="LONDON", utc_hour=9,
        analytics={"regime": {"label": "bull", "adx": 30.0, "atr_pct": 0.2},
                   "hurst": {"value": 0.62}, "kalman": {"slope": 0.4},
                   "vwap_deviation": {"z": 1.1},
                   "knn": {"win_rate": 0.6, "expectancy": 12.0},
                   "structure_magnet": {
                       "htf_alignment": "aligned",
                       "nearest_zone": {"dist_atr": 0.4, "side": "above", "inside": False},
                       "per_tf": {"1w": {"label": "bull", "premium_discount": 0.7,
                                         "nearest_fib": {"dist_atr": 0.3}}}}},
        ai_signal={"verdict": "approve", "confidence": 0.8, "score": 72.0},
        ai_exec={"verdict": "caution", "confidence": 0.6})


def test_spans_all_namespaces():
    fv = _full()
    ns = {k.split(".")[0] for k in fv}
    assert {"ta", "analytics", "struct", "magnet", "ai", "ctx"} <= ns


def test_namespaced_paths_are_correct():
    fv = _full()
    assert fv["ta.1h.rsi_14.value"] == 55.0
    assert fv["ta.1h.ema_200.above"] is True
    assert fv["analytics.regime.label"] == "bull"
    assert fv["analytics.hurst.value"] == 0.62
    assert fv["magnet.htf_alignment"] == "aligned"
    assert fv["magnet.nearest.side"] == "above"
    assert fv["struct.1w.label"] == "bull"
    assert fv["ai.signal.verdict"] == "approve"
    assert fv["ai.exec.verdict"] == "caution"
    assert fv["ctx.session"] == "LONDON" and fv["ctx.utc_hour"] == 9


def test_missing_layers_get_explicit_markers():
    fv = FV.assemble(ta_features={"1h": {"rsi_14": {"value": 40.0}}})
    assert fv["analytics.regime.label"] == "unknown"
    assert fv["magnet.htf_alignment"] == "unknown"
    assert fv["ai.signal.verdict"] == "none"
    # non-scalar values (lists/dicts) are never emitted
    assert all(isinstance(v, (bool, int, float, str)) for v in fv.values())


def test_flatten_passes_through_flat_vector():
    flat = B._flatten(_full())
    assert flat["analytics.regime.label"] == "bull"
    assert flat["ta.1h.ema_200.above"] is True
    # legacy nested TA dict still flattens
    legacy = B._flatten({"1h": {"rsi_14": {"value": 55}}})
    assert legacy["1h.rsi_14.value"] == 55


def test_model_sees_non_ta_namespaces():
    # synthetic set where the label + AI verdict track the win
    exs = []
    for i in range(30):
        win = i % 2 == 0
        exs.append((FV.assemble(
            ta_features={"1h": {"rsi_14": {"value": 50.0 + i}}},
            analytics={"regime": {"label": "bull" if win else "bear"}},
            ai_signal={"verdict": "approve" if win else "reject", "confidence": 0.7}), win))
    model = B.build_model(exs, min_n=3)
    ns = {c["condition"].split(".")[0] for c in model["conditions"]}
    non_ta = ns - {"ta"}
    assert len(non_ta) >= 2, non_ta       # proof: model now grades on >TA


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok ", n)
    print("ALL PASS")
