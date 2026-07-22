from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.db.models import Event
from beacon_core.db.filters import scope_events_to_account
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/events", tags=["events"], dependencies=[Depends(require_token)])


@router.get("")
async def list_events(trade_id: int | None = None, leg_id: int | None = None,
                      kind: str | None = None, account_id: int | None = None,
                      limit: int = 200,
                      db: AsyncSession = Depends(get_db)):
    """The append-only execution/audit log — every decision and broker
    interaction, newest first. This is the execution-workflow view."""
    q = select(Event).order_by(Event.id.desc()).limit(limit)
    if trade_id is not None:
        q = q.where(Event.trade_id == trade_id)
    if leg_id is not None:
        q = q.where(Event.leg_id == leg_id)
    if kind:
        q = q.where(Event.kind == kind)
    q = scope_events_to_account(q, account_id)   # #118 honor the global account filter
    rows = (await db.execute(q)).scalars().all()
    return [{"id": e.id, "trade_id": e.trade_id, "leg_id": e.leg_id,
             "kind": e.kind, "payload": e.payload,
             "ts": e.ts.isoformat() if e.ts else None} for e in rows]
