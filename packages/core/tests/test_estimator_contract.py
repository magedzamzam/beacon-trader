"""Estimator output contract (#70) — the locked envelope + central mapper.

Pure, bare-box. Proves (1) the Estimator Protocol accepts a plain callable,
(2) the single `estimator_contributions` mapper yields the common
(name,value,direction,weight,confidence) shape for every estimator, and
(3) the sidecar harness attaches a uniform `_contributions` block WITHOUT
altering the per-estimator detail dicts (back-compat)."""
import asyncio

from beacon_core.analysis.contract import (Estimator, EstimatorResult,
                                           feature_contribution,
                                           estimator_contributions)
from beacon_core.analysis import structure as structure_mod


def test_protocol_accepts_callable():
    def est(ctx):
        return {"ok": True}
    assert isinstance(est, Estimator)          # runtime_checkable structural match
    assert EstimatorResult is not None


def test_structure_reexports_single_definition():
    # structure.feature_contribution must BE the canonical contract one (#70).
    assert structure_mod.feature_contribution is feature_contribution


def test_contribution_shape_keys():
    c = feature_contribution("x", 1.0, "up", 0.5, 0.9)
    assert set(c) == {"name", "value", "direction", "weight", "confidence"}
    assert c["weight"] == 0.5 and c["confidence"] == 0.9


def test_mapper_covers_every_estimator():
    cases = {
        "regime": {"label": "trend", "adx": 30},
        "hurst": {"value": 0.62},
        "kalman": {"slope": -0.4},
        "vwap_deviation": {"z": 1.5},
        "knn": {"win_rate": 0.55},
        "structure_magnet": {"htf_alignment": "with",
                             "nearest_zone": {"dist_atr": 0.3, "side": "above"}},
    }
    for name, detail in cases.items():
        cs = estimator_contributions(name, detail)
        assert cs, f"{name} produced no contribution"
        for c in cs:
            assert set(c) == {"name", "value", "direction", "weight", "confidence"}
    # directions are derived, not guessed
    assert estimator_contributions("kalman", {"slope": -0.4})[0]["direction"] == "down"
    assert estimator_contributions("vwap_deviation", {"z": 1.5})[0]["direction"] == "above"
    assert estimator_contributions("hurst", {"value": 0.62})[0]["direction"] == "trending"
    # magnet emits alignment + zone
    assert len(estimator_contributions("structure_magnet", cases["structure_magnet"])) == 2


def test_mapper_is_defensive():
    assert estimator_contributions("regime", None) == []
    assert estimator_contributions("unknown_estimator", {"x": 1}) == []
    assert estimator_contributions("hurst", {}) == []           # missing value -> []
    assert estimator_contributions("knn", {"win_rate": None}) == []


def test_harness_attaches_contributions_without_touching_detail():
    from beacon_core.analysis import sidecar

    def regime(ctx):
        return {"label": "trend", "adx": 27.0}
    def hurst(ctx):
        return {"value": 0.7}

    analytics, degraded = asyncio.run(
        sidecar.run_estimators(object(), {"regime": regime, "hurst": hurst}))

    assert degraded == []
    # per-estimator detail dicts are IDENTICAL to what the estimator returned
    assert analytics["regime"] == {"label": "trend", "adx": 27.0}
    assert analytics["hurst"] == {"value": 0.7}
    # additive uniform block present, in the common shape
    contribs = analytics["_contributions"]
    names = {c["name"] for c in contribs}
    assert {"regime", "hurst"} <= names
    for c in contribs:
        assert set(c) == {"name", "value", "direction", "weight", "confidence"}


def test_harness_no_contributions_when_empty():
    from beacon_core.analysis import sidecar
    analytics, _ = asyncio.run(sidecar.run_estimators(object(), {}))
    assert "_contributions" not in analytics       # no estimators -> no block


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok ", n)
    print("ALL PASS")
