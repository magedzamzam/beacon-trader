from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.db.models import Leg, Signal, Source, Trade
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/performance", tags=["performance"],
                   dependencies=[Depends(require_token)])


@router.get("/summary")
async def summary(db: AsyncSession = Depends(get_db)):
    closed = (await db.execute(select(Leg).where(Leg.status == "closed"))).scalars().all()
    wins = [l for l in closed if l.outcome == "tp_hit"]
    losses = [l for l in closed if l.outcome == "sl_hit"]
    total_pl = sum((float(l.realized_pl) for l in closed if l.realized_pl is not None), 0.0)
    win_rate = (len(wins) / len(closed) * 100.0) if closed else 0.0
    gross_win = sum((float(l.realized_pl) for l in wins if l.realized_pl), 0.0)
    gross_loss = abs(sum((float(l.realized_pl) for l in losses if l.realized_pl), 0.0))
    pf = (gross_win / gross_loss) if gross_loss else None
    return {"total_pl": round(total_pl, 2), "win_rate": round(win_rate, 2),
            "closed_legs": len(closed), "wins": len(wins), "losses": len(losses),
            "profit_factor": round(pf, 2) if pf else None}


@router.get("/by_source")
async def by_source(db: AsyncSession = Depends(get_db)):
    """Per-source: realized P&L and per-TP hit counts — the table that tells you
    which channel reaches TP1 reliably vs stalls before TP3."""
    q = (select(Source.id, Source.name,
                Leg.tp_index, Leg.outcome,
                func.count(Leg.id), func.coalesce(func.sum(Leg.realized_pl), 0))
         .select_from(Leg)
         .join(Trade, Trade.id == Leg.trade_id)
         .join(Signal, Signal.id == Trade.signal_id)
         .join(Source, Source.id == Signal.source_id)
         .where(Leg.status == "closed")
         .group_by(Source.id, Source.name, Leg.tp_index, Leg.outcome))
    rows = (await db.execute(q)).all()
    agg: dict = {}
    for sid, sname, tp_index, outcome, cnt, pl in rows:
        s = agg.setdefault(sid, {"source_id": sid, "name": sname, "pl": 0.0,
                                 "tp_hits": {}, "sl_hits": 0})
        s["pl"] += float(pl)
        if outcome == "tp_hit":
            s["tp_hits"][tp_index] = s["tp_hits"].get(tp_index, 0) + cnt
        elif outcome == "sl_hit":
            s["sl_hits"] += cnt
    return list(agg.values())
