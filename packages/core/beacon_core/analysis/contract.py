"""Shared estimator output contract (#70).

The sidecar estimators (`estimators.py`) each return an ad-hoc dict. This module
LOCKS the envelope before Phase-2/3 estimators (#54/#55/#56) land, so the
feature-vector assembler and bayes can consume every estimator uniformly instead
of special-casing each shape.

Design choice — NON-BREAKING: estimators keep returning their rich `detail` dict
(persisted unchanged in `SignalAnalytics.analytics`, so #62/#53 readers and the
internal k-NN are untouched). The standard envelope is realized at the harness
layer: `estimator_contributions(name, detail)` is the SINGLE place that maps each
estimator's shape to the common `(name, value, direction, weight, confidence)`
contribution, and the sidecar attaches the collected list as `analytics
["_contributions"]` for a future uniform consumer.
"""
from __future__ import annotations

from typing import List, Optional, Protocol, runtime_checkable

# The common feature-contribution shape every layer emits (structure/magnets,
# regime, TA, bayes). Kept as a plain dict for JSON-persistence.
FeatureContribution = dict          # {name, value, direction, weight, confidence}

# One estimator result: the rich per-estimator dict (what's persisted today).
EstimatorResult = Optional[dict]


@runtime_checkable
class Estimator(Protocol):
    """The estimator call contract: a callable of the analytics context returning
    a JSON-able dict (or None). May be sync or async (the harness awaits if so)."""
    def __call__(self, ctx) -> object: ...


def feature_contribution(name: str, value, direction: Optional[str],
                         weight: float, confidence: float) -> FeatureContribution:
    """The shared (name, value, direction, weight, confidence) envelope — identical
    across structure/magnet, regime, TA, and bayes so a unified engine composes
    one score across layers. This is the single definition (structure.py re-exports)."""
    return {"name": name, "value": value, "direction": direction,
            "weight": float(weight), "confidence": float(confidence)}


def _sign(v, up="up", down="down") -> Optional[str]:
    if v is None:
        return None
    return up if v > 0 else down if v < 0 else None


def estimator_contributions(name: str, detail) -> List[FeatureContribution]:
    """Map one estimator's persisted `detail` dict to standard contributions —
    the SINGLE place that knows each estimator's shape (#70). Unknown estimators
    and non-dict details yield []."""
    if not isinstance(detail, dict):
        return []
    fc = feature_contribution
    if name == "regime":
        return [fc("regime", detail.get("label"), None, 1.0, 1.0)]
    if name == "hurst":
        v = detail.get("value")
        return [fc("hurst", v, _sign(None if v is None else v - 0.5, "trending", "meanrev"), 1.0, 1.0)] if v is not None else []
    if name == "kalman":
        s = detail.get("slope")
        return [fc("kalman_slope", s, _sign(s), 1.0, 1.0)] if s is not None else []
    if name == "vwap_deviation":
        z = detail.get("z")
        return [fc("vwap_z", z, _sign(z, "above", "below"), 1.0, 1.0)] if z is not None else []
    if name == "knn":
        wr = detail.get("win_rate")
        return [fc("knn_win_rate", wr, None, 1.0, 1.0)] if wr is not None else []
    if name == "structure_magnet":
        out = [fc("htf_alignment", detail.get("htf_alignment"), None, 1.0, 1.0)]
        nz = detail.get("nearest_zone") or {}
        if nz.get("dist_atr") is not None:
            out.append(fc("magnet_dist_atr", nz.get("dist_atr"), nz.get("side"), 1.0, 1.0))
        return out
    return []
