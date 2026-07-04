from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.db.models import (AiAssessment, Event, Leg, PositionActivity,
                                   SignalFeature, Trade)
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/trades", tags=["trades"], dependencies=[Depends(require_token)])


@router.get("")
async def list_trades(db: AsyncSession = Depends(get_db), limit: int = 100):
    rows = (await db.execute(select(Trade).order_by(Trade.id.desc()).limit(limit))).scalars().all()
    out = []
    for t in rows:
        legs = (await db.execute(select(Leg).where(Leg.trade_id == t.id))).scalars().all()
        out.append({
            "id": t.id, "signal_id": t.signal_id, "account_id": t.account_id,
            "symbol": t.symbol, "direction": t.direction, "status": t.status,
            "planned_risk": float(t.planned_risk) if t.planned_risk else None,
            "realized_pl": float(t.realized_pl),
            "legs": [{"id": l.id, "tp_index": l.tp_index, "order_type": l.order_type,
                      "entry": float(l.entry), "tp": float(l.tp), "sl": float(l.sl),
                      "lot": float(l.lot), "status": l.status, "outcome": l.outcome,
                      "sl_moved": l.sl_moved,
                      "realized_pl": float(l.realized_pl) if l.realized_pl is not None else None}
                     for l in legs]})
    return out


@router.get("/{trade_id}")
async def trade_detail(trade_id: int, db: AsyncSession = Depends(get_db)):
    """One trade with its legs, execution-workflow events, and AI assessments."""
    t = await db.get(Trade, trade_id)
    if not t:
        raise HTTPException(404, "trade not found")
    legs = (await db.execute(select(Leg).where(Leg.trade_id == t.id))).scalars().all()
    events = (await db.execute(select(Event).where(Event.trade_id == t.id)
                               .order_by(Event.id))).scalars().all()
    ai = (await db.execute(select(AiAssessment).where(AiAssessment.trade_id == t.id)
                           .order_by(AiAssessment.id.desc()))).scalars().all()
    acts = (await db.execute(select(PositionActivity).where(PositionActivity.trade_id == t.id)
                             .order_by(PositionActivity.activity_at.desc(),
                                       PositionActivity.id.desc()))).scalars().all()
    feat = (await db.execute(select(SignalFeature).where(
        SignalFeature.signal_id == t.signal_id))).scalars().first()
    return {
        "id": t.id, "signal_id": t.signal_id, "account_id": t.account_id,
        "symbol": t.symbol, "direction": t.direction, "status": t.status,
        "planned_risk": float(t.planned_risk) if t.planned_risk else None,
        "realized_pl": float(t.realized_pl),
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "legs": [{"id": l.id, "tp_index": l.tp_index, "order_type": l.order_type,
                  "entry": float(l.entry), "tp": float(l.tp), "sl": float(l.sl),
                  "lot": float(l.lot), "status": l.status, "outcome": l.outcome,
                  "sl_moved": l.sl_moved,
                  "realized_pl": float(l.realized_pl) if l.realized_pl is not None else None}
                 for l in legs],
        "events": [{"id": e.id, "leg_id": e.leg_id, "kind": e.kind,
                    "payload": e.payload, "ts": e.ts.isoformat() if e.ts else None}
                   for e in events],
        "ai": [{"id": a.id, "kind": a.kind, "verdict": a.verdict,
                "confidence": float(a.confidence) if a.confidence is not None else None,
                "score": float(a.score) if a.score is not None else None,
                "rationale": a.rationale, "model": a.model,
                "created_at": a.created_at.isoformat() if a.created_at else None}
               for a in ai],
        "activities": [{"id": p.id, "leg_id": p.leg_id, "deal_id": p.deal_id,
                        "deal_reference": p.deal_reference, "epic": p.epic,
                        "source": p.source, "type": p.type, "status": p.status,
                        "realized_pl": float(p.realized_pl) if p.realized_pl is not None else None,
                        "currency": p.currency,
                        "at": p.activity_at.isoformat() if p.activity_at else None}
                       for p in acts],
        "features": ({"session": feat.session, "utc_hour": feat.utc_hour,
                      "price": float(feat.price) if feat.price is not None else None,
                      "captured_at": feat.captured_at.isoformat() if feat.captured_at else None,
                      "timeframes": feat.features or {}} if feat else None),
    }
