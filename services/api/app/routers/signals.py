from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.db.models import Signal, Source
from beacon_core.parsing import parse
from ..deps import get_db
from ..auth import require_token
from ..schemas import ManualSignalIn
from ._ingest import ingest_structured

router = APIRouter(tags=["signals"])


@router.get("/signals", dependencies=[Depends(require_token)])
async def list_signals(db: AsyncSession = Depends(get_db), limit: int = 100):
    rows = (await db.execute(select(Signal).order_by(Signal.id.desc()).limit(limit))).scalars().all()
    return [{"id": s.id, "source_id": s.source_id, "symbol": s.symbol,
             "direction": s.direction, "entry_from": float(s.entry_from),
             "entry_to": float(s.entry_to), "sl": float(s.sl), "tps": s.tps,
             "order_type": s.order_type, "status": s.status,
             "reject_reason": s.reject_reason,
             "created_at": s.created_at.isoformat() if s.created_at else None}
            for s in rows]


@router.post("/signals/manual", dependencies=[Depends(require_token)])
async def manual_signal(body: ManualSignalIn, db: AsyncSession = Depends(get_db)):
    sid, ok, reason = await ingest_structured(
        db, source_id=body.source_id, symbol=body.symbol, direction=body.direction,
        entry_from=body.entry_from, entry_to=body.entry_to, sl=body.sl,
        tps=body.tps, order_type=body.order_type, raw_text="manual")
    return {"signal_id": sid, "accepted": ok, "reason": reason}


@router.post("/ingest/tv/{key}")
async def tradingview_webhook(key: str, request: Request, db: AsyncSession = Depends(get_db)):
    """TradingView alert / generic API webhook, authenticated by the source's
    external_id used as an API key. Accepts JSON (structured) or {"text": "..."}."""
    src = (await db.execute(select(Source).where(
        Source.external_id == key, Source.kind.in_(("tradingview", "api"))))).scalar_one_or_none()
    if not src:
        raise HTTPException(401, "unknown ingest key")
    body = await request.json()

    if "text" in body:                       # free-text alert -> parse
        parsed = parse(body["text"])
        if not parsed:
            raise HTTPException(422, "could not parse signal text")
        sid, ok, reason = await ingest_structured(
            db, source_id=src.id, symbol=parsed.symbol, direction=parsed.direction,
            entry_from=parsed.entry_from, entry_to=parsed.entry_to, sl=parsed.sl,
            tps=parsed.tps, order_type=parsed.order_type_hint or "MARKET",
            raw_text=body["text"])
    else:                                    # structured JSON
        sid, ok, reason = await ingest_structured(
            db, source_id=src.id, symbol=body["symbol"], direction=body["direction"],
            entry_from=body["entry_from"], entry_to=body.get("entry_to", body["entry_from"]),
            sl=body["sl"], tps=body["tps"], order_type=body.get("order_type", "MARKET"),
            raw_text="tv")
    return {"signal_id": sid, "accepted": ok, "reason": reason}
