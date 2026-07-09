from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.analysis import bayes as B
from beacon_core.db.models import SignalFeature, Trade
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/analysis", tags=["analysis"],
                   dependencies=[Depends(require_token)])


async def _model(db: AsyncSession, min_n: int):
    """Label closed trades by realized P&L > 0, join to their captured signal
    features, and build the Bayesian model."""
    trades = (await db.execute(select(Trade).where(Trade.status == "closed"))).scalars().all()
    sfs = (await db.execute(select(SignalFeature))).scalars().all()
    by_sig = {s.signal_id: s.features for s in sfs if s.features}
    examples = [(by_sig[t.signal_id], float(t.realized_pl or 0) > 0)
                for t in trades if t.signal_id in by_sig]
    return B.build_model(examples, min_n=min_n)


def _round_cond(c: dict) -> dict:
    return {**c, "mean": round(c["mean"], 4), "ci_low": round(c["ci_low"], 4),
            "ci_high": round(c["ci_high"], 4), "raw_wr": round(c["raw_wr"], 4),
            "lift": round(c["lift"], 4)}


@router.get("/bayes")
async def bayes(min_n: int = 5, limit: int = 100, db: AsyncSession = Depends(get_db)):
    """Per-condition Beta-Binomial win-rate table (credible intervals shrink thin
    samples toward the base rate) + Naive-Bayes P(win) for recent signals."""
    model = await _model(db, min_n)
    if not model.get("ready"):
        return {"ready": False, "n": 0,
                "message": "No closed trades with captured features yet."}

    recent = []
    rows = (await db.execute(select(SignalFeature)
                             .order_by(SignalFeature.id.desc()).limit(30))).scalars().all()
    for sf in rows:
        sc = B.score(model, sf.features or {})
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
    sf = (await db.execute(select(SignalFeature).where(
        SignalFeature.signal_id == signal_id))).scalars().first()
    if not sf:
        raise HTTPException(404, "no captured features for this signal")
    sc = B.score(model, sf.features or {})
    return {"signal_id": signal_id, "ready": bool(model.get("ready")),
            "base_rate": round(model.get("base_rate", 0.0), 4) if model.get("ready") else None,
            "score": (None if not sc else
                      {"p_win": round(sc["p_win"], 4), "contributors": sc["contributors"]})}
