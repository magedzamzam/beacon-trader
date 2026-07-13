"""Unified per-signal feature vector (#62).

The Bayesian model used to see only the TA snapshot. This assembles ONE flat,
namespaced feature dict per signal spanning every layer we already capture — TA,
the analytics estimators, the structure/magnet reference (#61), AI verdicts, and
session/time — so `bayes.build_model`/`score` can attribute predictive lift to
*which layer*, not just TA.

Point-in-time by construction: it reads the values that were **persisted at
signal time** (signal_features, signal_analytics, ai_assessments) — never a
fresh recompute — so the model can't leak future information.

Namespaces:
  ta.<tf>.<indicator>.<field>   analytics.<estimator>.<field>
  struct.<tf>.<field>           magnet.nearest.<field> / magnet.htf_alignment
  ai.signal.* / ai.exec.*       ctx.session / ctx.utc_hour

Missing-data policy: a degraded/absent layer is represented with an explicit
`unknown` / `none` marker (categorical) rather than silently dropping the signal.
"""
from __future__ import annotations

from typing import Optional


def _scalar(v) -> bool:
    return isinstance(v, (bool, int, float, str)) and not isinstance(v, bytes)


def _put(fv: dict, key: str, val) -> None:
    if val is not None and _scalar(val):
        fv[key] = val


def _dig(d, *path):
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def assemble(*, ta_features: dict = None, session_tag=None, utc_hour=None,
             analytics: dict = None, ai_signal: dict = None,
             ai_exec: dict = None) -> dict:
    """Pure assembler — plain dicts in, one flat namespaced dict out. All layers
    optional; absent categorical layers get an explicit marker."""
    fv: dict = {}

    # --- TA (per timeframe) : ta.<tf>.<indicator>.<field> ---
    for tf, inds in (ta_features or {}).items():
        if not isinstance(inds, dict):
            continue
        for key, val in inds.items():
            if key.startswith("_"):
                continue
            if isinstance(val, dict):
                for field, v in val.items():
                    _put(fv, f"ta.{tf}.{key}.{field}", v)
            else:
                _put(fv, f"ta.{tf}.{key}", val)

    # --- analytics estimators ---
    a = analytics or {}
    regime = a.get("regime") or {}
    _put(fv, "analytics.regime.label", regime.get("label"))
    _put(fv, "analytics.regime.adx", regime.get("adx"))
    _put(fv, "analytics.regime.atr_pct", regime.get("atr_pct"))
    _put(fv, "analytics.regime.realized_vol", regime.get("realized_vol"))
    _put(fv, "analytics.hurst.value", _dig(a, "hurst", "value"))
    _put(fv, "analytics.kalman.slope", _dig(a, "kalman", "slope"))
    _put(fv, "analytics.vwap.z", _dig(a, "vwap_deviation", "z"))
    knn = a.get("knn") or {}
    _put(fv, "analytics.knn.win_rate", knn.get("win_rate"))
    _put(fv, "analytics.knn.expectancy", knn.get("expectancy"))
    if not regime.get("label"):
        fv["analytics.regime.label"] = "unknown"        # explicit missing marker

    # --- structure / magnets (#61) ---
    sm = a.get("structure_magnet") or {}
    _put(fv, "magnet.htf_alignment", sm.get("htf_alignment"))
    nz = sm.get("nearest_zone") or {}
    _put(fv, "magnet.nearest.dist_atr", nz.get("dist_atr"))
    _put(fv, "magnet.nearest.side", nz.get("side"))
    _put(fv, "magnet.nearest.inside", nz.get("inside"))
    for tf, st in (sm.get("per_tf") or {}).items():
        if not isinstance(st, dict):
            continue
        _put(fv, f"struct.{tf}.label", st.get("label"))
        _put(fv, f"struct.{tf}.premium_discount", st.get("premium_discount"))
        _put(fv, f"struct.{tf}.fib_dist_atr", _dig(st, "nearest_fib", "dist_atr"))
    if not sm.get("htf_alignment"):
        fv["magnet.htf_alignment"] = "unknown"

    # --- AI verdicts ---
    if ai_signal:
        _put(fv, "ai.signal.verdict", ai_signal.get("verdict"))
        _put(fv, "ai.signal.confidence", ai_signal.get("confidence"))
        _put(fv, "ai.signal.score", ai_signal.get("score"))
    else:
        fv["ai.signal.verdict"] = "none"
    if ai_exec:
        _put(fv, "ai.exec.verdict", ai_exec.get("verdict"))
        _put(fv, "ai.exec.confidence", ai_exec.get("confidence"))

    # --- session / time ---
    _put(fv, "ctx.session", session_tag)
    _put(fv, "ctx.utc_hour", utc_hour)

    return fv


def from_rows(sf, sa, ai_signal_row=None, ai_exec_row=None) -> dict:
    """Assemble from ORM rows (SignalFeature, SignalAnalytics, AiAssessment)."""
    def _ai(row):
        if row is None:
            return None
        return {"verdict": row.verdict,
                "confidence": float(row.confidence) if row.confidence is not None else None,
                "score": float(row.score) if row.score is not None else None}

    return assemble(
        ta_features=(sf.features if sf is not None else None),
        session_tag=(sf.session if sf is not None else None),
        utc_hour=(sf.utc_hour if sf is not None else None),
        analytics=(sa.analytics if sa is not None else None),
        ai_signal=_ai(ai_signal_row), ai_exec=_ai(ai_exec_row))


async def feature_vector(session, signal_id: int) -> Optional[dict]:
    """The unified feature vector for one signal, or None if nothing was captured.
    Reads persisted rows only (point-in-time)."""
    from sqlalchemy import select
    from ..db.models import SignalFeature, SignalAnalytics, AiAssessment

    sf = (await session.execute(select(SignalFeature).where(
        SignalFeature.signal_id == signal_id))).scalar_one_or_none()
    sa = (await session.execute(select(SignalAnalytics).where(
        SignalAnalytics.signal_id == signal_id))).scalar_one_or_none()
    ai_rows = (await session.execute(select(AiAssessment).where(
        AiAssessment.signal_id == signal_id).order_by(AiAssessment.id))).scalars().all()
    if sf is None and sa is None:
        return None
    ai_signal = next((r for r in reversed(ai_rows) if r.kind == "signal_validation"), None)
    ai_exec = next((r for r in reversed(ai_rows) if r.kind == "execution_review"), None)
    return from_rows(sf, sa, ai_signal, ai_exec)
