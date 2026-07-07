from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.db.models import Signal, Source, TelegramMessage
from ..deps import get_db
from ..auth import require_token
from ..schemas import SourceIn

router = APIRouter(prefix="/sources", tags=["sources"], dependencies=[Depends(require_token)])


def _dump(s: Source) -> dict:
    return {"id": s.id, "kind": s.kind, "name": s.name, "external_id": s.external_id,
            "enabled_for_trading": s.enabled_for_trading, "is_trusted": s.is_trusted,
            "strategy": s.strategy, "risk_config": s.risk_config,
            "account_map": s.account_map}


@router.get("")
async def list_sources(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Source))).scalars().all()
    return [_dump(s) for s in rows]


@router.post("")
async def create_source(body: SourceIn, db: AsyncSession = Depends(get_db)):
    s = Source(**body.model_dump()); db.add(s); await db.commit()
    return {"id": s.id}


@router.patch("/{source_id}")
async def update_source(source_id: int, body: dict, db: AsyncSession = Depends(get_db)):
    s = await db.get(Source, source_id)
    for k in ("name", "enabled_for_trading", "is_trusted", "strategy",
              "risk_config", "account_map", "external_id"):
        if k in body:
            setattr(s, k, body[k])
    await db.commit()
    return _dump(s)


@router.delete("/{source_id}")
async def delete_source(source_id: int, db: AsyncSession = Depends(get_db)):
    s = await db.get(Source, source_id)
    if s:
        # Detach dependents first — signals and telegram messages FK-reference the
        # source, so a hard delete would otherwise fail. Both columns are nullable,
        # so this preserves the history (and the trades behind those signals) while
        # letting the source be removed.
        await db.execute(update(Signal).where(Signal.source_id == source_id)
                         .values(source_id=None))
        await db.execute(update(TelegramMessage).where(TelegramMessage.source_id == source_id)
                         .values(source_id=None))
        await db.delete(s)
        await db.commit()
    return {"ok": True}
