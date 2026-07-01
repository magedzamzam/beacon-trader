from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.db.models import SymbolMap
from ..deps import get_db
from ..auth import require_token
from ..schemas import SymbolMapIn

router = APIRouter(prefix="/symbols", tags=["symbols"], dependencies=[Depends(require_token)])


@router.get("")
async def list_symbols(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(SymbolMap))).scalars().all()
    return [{"id": s.id, "broker_id": s.broker_id, "internal_symbol": s.internal_symbol,
             "broker_epic": s.broker_epic, "value_per_point": float(s.value_per_point),
             "min_lot": float(s.min_lot), "lot_step": float(s.lot_step),
             "min_stop_distance": float(s.min_stop_distance) if s.min_stop_distance else None}
            for s in rows]


@router.post("")
async def create_symbol(body: SymbolMapIn, db: AsyncSession = Depends(get_db)):
    s = SymbolMap(**body.model_dump()); db.add(s); await db.commit()
    return {"id": s.id}
