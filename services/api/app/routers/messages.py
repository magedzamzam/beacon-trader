from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.bus import Bus
from beacon_core.config import CH_TG_CONTROL
from beacon_core.db.models import Source, TelegramMessage
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/messages", tags=["messages"], dependencies=[Depends(require_token)])
bus = Bus()


@router.get("")
async def list_messages(source_id: int | None = None, chat_id: str | None = None,
                        only_signals: bool = False, limit: int = 200,
                        db: AsyncSession = Depends(get_db)):
    """Full, persisted Telegram history — every message on watched channels,
    signal or not — newest first."""
    q = (select(TelegramMessage, Source.name)
         .outerjoin(Source, Source.id == TelegramMessage.source_id)
         .order_by(TelegramMessage.id.desc()).limit(limit))
    if source_id is not None:
        q = q.where(TelegramMessage.source_id == source_id)
    if chat_id is not None:
        q = q.where(TelegramMessage.chat_id == chat_id)
    if only_signals:
        q = q.where(TelegramMessage.is_signal == True)  # noqa: E712
    rows = (await db.execute(q)).all()
    return [{
        "id": m.id, "source_id": m.source_id, "source_name": sname or "—",
        "chat_id": m.chat_id, "sender": m.sender, "text": m.text,
        "is_signal": m.is_signal, "parse_status": m.parse_status,
        "reject_reason": m.reject_reason, "signal_id": m.signal_id,
        "message_date": m.message_date.isoformat() if m.message_date else None,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    } for (m, sname) in rows]


@router.get("/channels")
async def channels(db: AsyncSession = Depends(get_db)):
    """Per-channel message + signal counts — the 'signals per channel' overview."""
    signal_count = func.sum(case((TelegramMessage.is_signal == True, 1), else_=0))  # noqa: E712
    q = (select(TelegramMessage.source_id, Source.name, Source.external_id,
                func.count(TelegramMessage.id), signal_count)
         .outerjoin(Source, Source.id == TelegramMessage.source_id)
         .group_by(TelegramMessage.source_id, Source.name, Source.external_id))
    rows = (await db.execute(q)).all()
    return [{"source_id": sid, "name": name or "—", "external_id": ext,
             "messages": int(total or 0), "signals": int(sig or 0)}
            for (sid, name, ext, total, sig) in rows]


@router.post("/sync")
async def sync_history(limit: int = 200):
    """Ask the telegram worker to backfill recent channel history now."""
    await bus.publish(CH_TG_CONTROL, {"action": "backfill", "limit": int(limit)})
    return {"ok": True, "requested": limit}
