from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.db.models import Source
from ..deps import get_db
from ..auth import require_token
from ..schemas import SourceIn

router = APIRouter(prefix="/sources", tags=["sources"], dependencies=[Depends(require_token)])


def _dump(s: Source) -> dict:
    return {"id": s.id, "kind": s.kind, "name": s.name, "external_id": s.external_id,
            "enabled_for_trading": s.enabled_for_trading, "is_trusted": s.is_trusted,
            "archived": bool(s.archived),
            "strategy": s.strategy, "risk_config": s.risk_config,
            "account_map": s.account_map}


@router.get("")
async def list_sources(include_archived: bool = False,
                       db: AsyncSession = Depends(get_db)):
    q = select(Source)
    if not include_archived:                       # archived are hidden from active lists
        q = q.where(Source.archived.is_(False))
    rows = (await db.execute(q.order_by(Source.id))).scalars().all()
    return [_dump(s) for s in rows]


@router.post("")
async def create_source(body: SourceIn, db: AsyncSession = Depends(get_db)):
    s = Source(**body.model_dump()); db.add(s); await db.commit()
    return {"id": s.id}


@router.patch("/{source_id}")
async def update_source(source_id: int, body: dict, db: AsyncSession = Depends(get_db)):
    s = await db.get(Source, source_id)
    for k in ("name", "enabled_for_trading", "is_trusted", "strategy",
              "risk_config", "account_map", "external_id", "archived"):
        if k in body:
            setattr(s, k, body[k])
    await db.commit()
    return _dump(s)


@router.delete("/{source_id}")
async def delete_source(source_id: int, db: AsyncSession = Depends(get_db)):
    """Soft-delete: archive the source and stop it trading, but KEEP the row so
    its signals/trades keep their attribution (hard-delete would null source_id
    on every past trade and silently drop that P&L from per-source rollups — #20).
    Un-archive from the API with PATCH {"archived": false}."""
    s = await db.get(Source, source_id)
    if s:
        s.archived = True
        s.enabled_for_trading = False              # an archived source never trades
        await db.commit()
    return {"ok": True, "archived": True}
