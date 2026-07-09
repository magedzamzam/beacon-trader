import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.analysis import bayes as B
from beacon_core.db.models import AiAssessment, Event, Signal, SignalFeature, Trade
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/analysis", tags=["analysis"],
                   dependencies=[Depends(require_token)])


def _parse_dt(s):
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def _stats(xs):
    xs = sorted(x for x in xs if x is not None and x >= 0)
    if not xs:
        return None
    n = len(xs)
    def pct(p):
        return xs[min(n - 1, int(p * n))]
    return {"n": n, "avg": round(sum(xs) / n, 2), "median": round(pct(0.5), 2),
            "p90": round(pct(0.9), 2), "min": round(xs[0], 2), "max": round(xs[-1], 2)}


@router.get("/latency")
async def latency(date_from: str = None, date_to: str = None,
                  db: AsyncSession = Depends(get_db)):
    """Signal -> order latency, in seconds, from existing timestamps:
      total  = first `placed` event  -  signal.created_at
      ai     = signal_validation assessment  -  signal.created_at (0 when AI off)
    Use with a date range to compare before/after toggling AI validation."""
    frm, to = _parse_dt(date_from), _parse_dt(date_to)
    sq = select(Signal.id, Signal.created_at)
    if frm is not None:
        sq = sq.where(Signal.created_at >= frm)
    if to is not None:
        sq = sq.where(Signal.created_at < to)
    created = {sid: c for sid, c in (await db.execute(sq)).all()}
    if not created:
        return {"total": None, "ai": None, "n_signals": 0, "n_placed": 0}

    ids = list(created)
    placed = dict((await db.execute(
        select(Trade.signal_id, func.min(Event.ts))
        .join(Event, Event.trade_id == Trade.id)
        .where(Event.kind == "placed", Trade.signal_id.in_(ids))
        .group_by(Trade.signal_id))).all())
    ai_done = dict((await db.execute(
        select(AiAssessment.signal_id, func.max(AiAssessment.created_at))
        .where(AiAssessment.kind == "signal_validation", AiAssessment.signal_id.in_(ids))
        .group_by(AiAssessment.signal_id))).all())

    totals, ais = [], []
    for sid, c in created.items():
        if sid in placed and placed[sid] and c:
            totals.append((placed[sid] - c).total_seconds())
        if sid in ai_done and ai_done[sid] and c:
            ais.append((ai_done[sid] - c).total_seconds())
    return {"total": _stats(totals), "ai": _stats(ais),
            "n_signals": len(created), "n_placed": len(placed)}


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
