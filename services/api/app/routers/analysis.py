from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.analysis import bayes as B
from beacon_core.analysis import feature_vector as FV
from beacon_core.db.models import SignalFeature, SignalAnalytics, AiAssessment, Trade
from beacon_core.timeutil import parse_iso_utc
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/analysis", tags=["analysis"],
                   dependencies=[Depends(require_token)])


async def _model(db: AsyncSession, min_n: int, frm=None, to=None):
    """Label closed trades by realized P&L > 0 and build the Bayesian model over
    the UNIFIED per-signal feature vector (#62) — TA + analytics + structure/
    magnets + AI + session — not just the TA snapshot. Optional [frm, to) window
    (#58) anchored on Trade.created_at (entry/signal time; Trade has no close ts).
    Point-in-time: reads persisted per-signal rows, never a fresh recompute."""
    q = select(Trade).where(Trade.status == "closed")
    if frm is not None:
        q = q.where(Trade.created_at >= frm)
    if to is not None:
        q = q.where(Trade.created_at < to)
    trades = (await db.execute(q)).scalars().all()

    # Bulk-load every layer once, keyed by signal_id (avoids N queries).
    sf_by = {s.signal_id: s for s in (await db.execute(select(SignalFeature))).scalars().all()}
    sa_by = {s.signal_id: s for s in (await db.execute(select(SignalAnalytics))).scalars().all()}
    ai_sig, ai_exec = {}, {}
    for r in (await db.execute(select(AiAssessment).where(
            AiAssessment.signal_id.isnot(None)).order_by(AiAssessment.id))).scalars().all():
        if r.kind == "signal_validation":
            ai_sig[r.signal_id] = r          # keep the latest (ordered by id)
        elif r.kind == "execution_review":
            ai_exec[r.signal_id] = r

    examples = []
    for t in trades:
        sid = t.signal_id
        if sid not in sf_by and sid not in sa_by:   # nothing captured -> skip the row
            continue
        fv = FV.from_rows(sf_by.get(sid), sa_by.get(sid), ai_sig.get(sid), ai_exec.get(sid))
        examples.append((fv, float(t.realized_pl or 0) > 0))
    return B.build_model(examples, min_n=min_n)


def _round_cond(c: dict) -> dict:
    return {**c, "mean": round(c["mean"], 4), "ci_low": round(c["ci_low"], 4),
            "ci_high": round(c["ci_high"], 4), "raw_wr": round(c["raw_wr"], 4),
            "lift": round(c["lift"], 4)}


@router.get("/bayes")
async def bayes(min_n: int = 5, limit: int = 100, date_from: str = None,
                date_to: str = None, db: AsyncSession = Depends(get_db)):
    """Per-condition Beta-Binomial win-rate table (credible intervals shrink thin
    samples toward the base rate) + Naive-Bayes P(win) for recent signals.
    Optional date range (#58) anchored on trade entry time."""
    model = await _model(db, min_n, parse_iso_utc(date_from), parse_iso_utc(date_to))
    if not model.get("ready"):
        return {"ready": False, "n": 0,
                "message": "No closed trades with captured features yet."}

    recent = []
    rows = (await db.execute(select(SignalFeature)
                             .order_by(SignalFeature.id.desc()).limit(30))).scalars().all()
    for sf in rows:
        fv = await FV.feature_vector(db, sf.signal_id)      # unified vector (#62)
        sc = B.score(model, fv or {})
        recent.append({"signal_id": sf.signal_id, "symbol": sf.symbol,
                       "direction": sf.direction,
                       "p_win": round(sc["p_win"], 4) if sc else None,
                       "captured_at": sf.captured_at.isoformat() if sf.captured_at else None})

    return {"ready": True, "n": model["n"], "wins": model["wins"],
            "losses": model["losses"], "base_rate": round(model["base_rate"], 4),
            "min_n": model["min_n"],
            "conditions": [_round_cond(c) for c in model["conditions"][:limit]],
            "recent": recent}


@router.get("/bayes/score/{signal_id}")
async def score_signal(signal_id: int, min_n: int = 5, db: AsyncSession = Depends(get_db)):
    model = await _model(db, min_n)
    fv = await FV.feature_vector(db, signal_id)             # unified vector (#62)
    if fv is None:
        raise HTTPException(404, "no captured features for this signal")
    sc = B.score(model, fv)
    return {"signal_id": signal_id, "ready": bool(model.get("ready")),
            "base_rate": round(model.get("base_rate", 0.0), 4) if model.get("ready") else None,
            "score": (None if not sc else
                      {"p_win": round(sc["p_win"], 4), "contributors": sc["contributors"]})}
