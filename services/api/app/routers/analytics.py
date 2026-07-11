"""Shadow analytics sidecar API (#53): the signal↔channel↔regime correlation
report and per-signal analytics. Read-only observability — nothing here gates
or alters trading."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.analysis.report import channel_regime_report, structure_outcome_report
from beacon_core.analysis.sidecar import load_config
from beacon_core.db.models import SignalAnalytics
from beacon_core.settings_store import set_setting
from beacon_core.timeutil import parse_iso_utc
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/analytics", tags=["analytics"],
                   dependencies=[Depends(require_token)])


@router.get("/config")
async def get_config(db: AsyncSession = Depends(get_db)):
    return await load_config(db)


@router.put("/config")
async def put_config(body: dict, db: AsyncSession = Depends(get_db)):
    cfg = await load_config(db)
    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
    if body.get("timeframe"):
        cfg["timeframe"] = str(body["timeframe"])
    if "window_bars" in body:
        try:
            cfg["window_bars"] = max(30, min(500, int(body["window_bars"])))
        except (TypeError, ValueError):
            pass
    await set_setting(db, "analytics", cfg)
    return cfg


@router.get("/correlation")
async def correlation(date_from: str = None, date_to: str = None,
                      db: AsyncSession = Depends(get_db)):
    """Per-channel × regime performance (with credible intervals), regime mix by
    channel, and a win/loss feature read. Optional date range (#58) anchored on
    signal time."""
    return await channel_regime_report(db, parse_iso_utc(date_from), parse_iso_utc(date_to))


@router.get("/structure")
async def structure(date_from: str = None, date_to: str = None,
                    db: AsyncSession = Depends(get_db)):
    """FVG/OB-vs-outcome cut (#59): win-rate & expectancy when the entry sits
    inside an unfilled Fair Value Gap / unmitigated Order Block vs not, per
    channel and regime with credible intervals. Shadow only."""
    return await structure_outcome_report(db, parse_iso_utc(date_from), parse_iso_utc(date_to))


@router.get("/signal/{signal_id}")
async def signal_analytics(signal_id: int, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(SignalAnalytics).where(
        SignalAnalytics.signal_id == signal_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "no analytics for this signal")
    return {"signal_id": row.signal_id, "symbol": row.symbol,
            "direction": row.direction, "regime": row.regime,
            "analytics": row.analytics, "degraded": row.degraded,
            "window": row.window,
            "captured_at": row.captured_at.isoformat() if row.captured_at else None}
