from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.db.models import AiAssessment, Event, Signal, Source
from beacon_core.parsing import parse
from ..deps import get_db
from ..auth import require_token
from ..schemas import ManualSignalIn
from ._ingest import ingest_structured

router = APIRouter(tags=["signals"])


async def _latest_ai_by_signal(db, signal_ids, kind="signal_validation") -> dict:
    """Map signal_id -> latest AI assessment of a kind, for the given signals."""
    if not signal_ids:
        return {}
    rows = (await db.execute(
        select(AiAssessment)
        .where(AiAssessment.kind == kind, AiAssessment.signal_id.in_(signal_ids))
        .order_by(AiAssessment.id.desc()))).scalars().all()
    out: dict = {}
    for a in rows:                       # first seen per signal is the newest
        out.setdefault(a.signal_id, a)
    return out


@router.get("/signals", dependencies=[Depends(require_token)])
async def list_signals(db: AsyncSession = Depends(get_db), limit: int = 100,
                       source_id: int | None = None):
    q = (select(Signal, Source.name, Source.kind)
         .outerjoin(Source, Source.id == Signal.source_id)
         .order_by(Signal.id.desc()).limit(limit))
    if source_id is not None:
        q = q.where(Signal.source_id == source_id)
    rows = (await db.execute(q)).all()
    ai = await _latest_ai_by_signal(db, [s.id for (s, _, _) in rows])
    out = []
    for (s, sname, skind) in rows:
        a = ai.get(s.id)
        out.append({"id": s.id, "source_id": s.source_id,
                    "source_name": sname or "—", "source_kind": skind,
                    "symbol": s.symbol,
                    "direction": s.direction, "entry_from": float(s.entry_from),
                    "entry_to": float(s.entry_to), "sl": float(s.sl), "tps": s.tps,
                    "order_type": s.order_type, "status": s.status,
                    "reject_reason": s.reject_reason,
                    "ai_verdict": a.verdict if a else None,
                    "ai_confidence": float(a.confidence) if a and a.confidence is not None else None,
                    "created_at": s.created_at.isoformat() if s.created_at else None})
    return out


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
            raw_text=body["text"], from_freetext=True)
    else:                                    # structured JSON
        sid, ok, reason = await ingest_structured(
            db, source_id=src.id, symbol=body["symbol"], direction=body["direction"],
            entry_from=body["entry_from"], entry_to=body.get("entry_to", body["entry_from"]),
            sl=body["sl"], tps=body["tps"], order_type=body.get("order_type", "MARKET"),
            raw_text="tv")
    return {"signal_id": sid, "accepted": ok, "reason": reason}


@router.post("/signals/{signal_id}/reinitiate", dependencies=[Depends(require_token)])
async def reinitiate_signal(signal_id: int, db: AsyncSession = Depends(get_db)):
    """Re-open a stored signal as a fresh trade (#66). Rather than re-enqueue the
    same signal (which the #15 idempotency guard would block, and which the old
    code mis-sent to pub/sub instead of the durable queue), CLONE it into a new
    validated Signal linked via `reinitiated_from`, then ENQUEUE the clone onto
    the same durable queue the executor consumes. The clone runs through every
    live gate (trust, risk, AI); block reasons land as events in the Activity
    feed."""
    from beacon_core.bus import Bus
    from beacon_core.config import CH_SIGNAL_VALID
    orig = await db.get(Signal, signal_id)
    if not orig:
        raise HTTPException(404, "signal not found")
    clone = Signal(
        source_id=orig.source_id, symbol=orig.symbol, direction=orig.direction,
        entry_from=orig.entry_from, entry_to=orig.entry_to, sl=orig.sl,
        tps=list(orig.tps or []), order_type=orig.order_type, status="validated",
        raw_text=orig.raw_text, market_snapshot=dict(orig.market_snapshot or {}),
        dedupe_hash=f"reinit:{orig.id}:{orig.dedupe_hash or ''}"[:64],
        reinitiated_from=orig.id)
    db.add(clone)
    await db.flush()                                   # assign clone.id
    db.add(Event(kind="reinitiated", payload={
        "signal_id": clone.id, "reinitiated_from": orig.id, "symbol": orig.symbol}))
    await db.commit()
    await Bus().enqueue(CH_SIGNAL_VALID, {"signal_id": clone.id})   # durable queue, not pub/sub
    return {"ok": True, "signal_id": clone.id, "reinitiated_from": orig.id,
            "message": f"Re-initiated as signal #{clone.id} — placing fresh orders on the mapped accounts."}
