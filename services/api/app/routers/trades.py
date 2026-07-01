from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.db.models import Leg, Trade
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
