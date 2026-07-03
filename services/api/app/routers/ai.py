from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.ai import resolve_ai_config
from beacon_core.ai import service as ai_service
from beacon_core.crypto import encrypt, has_key
from beacon_core.db.models import AiAssessment, Signal, Source, Trade
from beacon_core.settings_store import get_setting, set_setting
from ..deps import get_db
from ..auth import require_token
from ..schemas import AiConfigIn

router = APIRouter(prefix="/ai", tags=["ai"], dependencies=[Depends(require_token)])


def _dump(a: AiAssessment) -> dict:
    return {
        "id": a.id, "kind": a.kind, "signal_id": a.signal_id,
        "trade_id": a.trade_id, "account_id": a.account_id,
        "provider": a.provider, "model": a.model, "verdict": a.verdict,
        "confidence": float(a.confidence) if a.confidence is not None else None,
        "score": float(a.score) if a.score is not None else None,
        "rationale": a.rationale, "payload": a.payload,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


@router.get("/config")
async def get_config(db: AsyncSession = Depends(get_db)):
    stored = await get_setting(db, ai_service.AI_SETTING_KEY, {})
    cfg = resolve_ai_config(stored)
    out = cfg.public_dict()
    out["has_secret_key"] = has_key()          # whether encryption is available
    return out


@router.put("/config")
async def put_config(body: AiConfigIn, db: AsyncSession = Depends(get_db)):
    stored = dict(await get_setting(db, ai_service.AI_SETTING_KEY, {}) or {})
    stored.update({
        "enabled": body.enabled, "provider": body.provider, "model": body.model,
        "validate_signals": body.validate_signals,
        "review_execution": body.review_execution,
        "analyze_outcomes": body.analyze_outcomes,
        "gate_execution": body.gate_execution,
        "min_confidence": body.min_confidence,
        "validation_model": body.validation_model,
        "validation_timeout_seconds": body.validation_timeout_seconds,
        "validation_thinking": body.validation_thinking,
    })
    if body.api_key:                            # only overwrite when a new key is sent
        if not has_key():
            raise HTTPException(400, "SECRET_KEY is not set; cannot store the API key encrypted")
        stored["api_key_enc"] = encrypt(body.api_key)
    await set_setting(db, ai_service.AI_SETTING_KEY, stored)
    return resolve_ai_config(stored).public_dict()


@router.get("/assessments")
async def list_assessments(kind: str | None = None, signal_id: int | None = None,
                           trade_id: int | None = None, limit: int = 100,
                           db: AsyncSession = Depends(get_db)):
    q = select(AiAssessment).order_by(AiAssessment.id.desc()).limit(limit)
    if kind:
        q = q.where(AiAssessment.kind == kind)
    if signal_id is not None:
        q = q.where(AiAssessment.signal_id == signal_id)
    if trade_id is not None:
        q = q.where(AiAssessment.trade_id == trade_id)
    rows = (await db.execute(q)).scalars().all()
    return [_dump(a) for a in rows]


@router.post("/signals/{signal_id}/assess")
async def assess_signal_now(signal_id: int, db: AsyncSession = Depends(get_db)):
    """Manually (re)run AI validation for a stored signal."""
    sig = await db.get(Signal, signal_id)
    if not sig:
        raise HTTPException(404, "signal not found")
    cfg = await ai_service.load_config(db)
    if not cfg.ready:
        raise HTTPException(400, "AI is not enabled or no API key is configured")
    source = await db.get(Source, sig.source_id) if sig.source_id else None
    # force-run even if the validate_signals toggle is off
    cfg.validate_signals = True
    row = await ai_service.assess_signal(db, sig, source, cfg=cfg)
    await db.commit()
    if row is None:
        raise HTTPException(502, "AI assessment could not be produced")
    return _dump(row)


@router.post("/trades/{trade_id}/assess")
async def assess_trade_now(trade_id: int, db: AsyncSession = Depends(get_db)):
    """Manually run AI outcome analysis for a trade."""
    from beacon_core.db.models import Leg
    trade = await db.get(Trade, trade_id)
    if not trade:
        raise HTTPException(404, "trade not found")
    cfg = await ai_service.load_config(db)
    if not cfg.ready:
        raise HTTPException(400, "AI is not enabled or no API key is configured")
    legs = (await db.execute(select(Leg).where(Leg.trade_id == trade.id))).scalars().all()
    sig = await db.get(Signal, trade.signal_id)
    source = await db.get(Source, sig.source_id) if sig and sig.source_id else None
    trade_dict = {
        "symbol": trade.symbol, "direction": trade.direction,
        "planned_risk": str(trade.planned_risk) if trade.planned_risk else None,
        "realized_pl": str(trade.realized_pl),
        "source_name": source.name if source else "unknown",
        "legs": [{"tp_index": l.tp_index, "outcome": l.outcome,
                  "entry": str(l.entry), "close_price": str(l.close_price) if l.close_price else None,
                  "realized_pl": str(l.realized_pl) if l.realized_pl is not None else None}
                 for l in legs],
    }
    cfg.analyze_outcomes = True
    row = await ai_service.assess_outcome(db, trade_dict, trade.id, cfg=cfg)
    await db.commit()
    if row is None:
        raise HTTPException(502, "AI assessment could not be produced")
    return _dump(row)
