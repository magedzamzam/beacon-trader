"""Shadow analytics sidecar API (#53): the signal↔channel↔regime correlation
report and per-signal analytics. Read-only observability — nothing here gates
or alters trading."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.analysis.report import (channel_regime_report,
                                         structure_outcome_report,
                                         structure_magnet_outcome_report,
                                         trend_alignment_outcome_report)
from beacon_core.analysis.sidecar import load_config
from beacon_core.analysis import structure_map as struct_map
from beacon_core.analysis.structure import DEFAULT_STRUCTURE
from beacon_core.db.models import SignalAnalytics
from beacon_core.execution.trend_filter import trend_filter_cfg
from beacon_core.settings_store import get_setting, set_setting
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


@router.get("/trend-alignment")
async def trend_alignment(date_from: str = None, date_to: str = None,
                          db: AsyncSession = Depends(get_db)):
    """Aligned-vs-counter split as a first-class metric (#72): win-rate, net PnL
    and expectancy for trend-aligned vs counter-trend entries, overall and per
    channel, with credible intervals. Classified from persisted signal_features
    using the SAME timeframe/EMA the live #48 filter gates on. Shadow only."""
    cfg = trend_filter_cfg(await get_setting(db, "entry_filters", {}))
    return await trend_alignment_outcome_report(
        db, parse_iso_utc(date_from), parse_iso_utc(date_to),
        timeframe=cfg["timeframe"], ema_period=int(cfg["ema_period"]))


@router.get("/structure/outcome")
async def structure_outcome(date_from: str = None, date_to: str = None,
                            db: AsyncSession = Depends(get_db)):
    """Phase-2 measurement (#61): win-rate & expectancy by HTF alignment / magnet
    proximity / adverse-side, with credible intervals. Shadow — informs Phase 3."""
    return await structure_magnet_outcome_report(db, parse_iso_utc(date_from),
                                                 parse_iso_utc(date_to))


@router.get("/structure/config")
async def structure_config(db: AsyncSession = Depends(get_db)):
    return await struct_map.load_config(db)


@router.put("/structure/config")
async def structure_config_put(body: dict, db: AsyncSession = Depends(get_db)):
    cfg = await struct_map.load_config(db)
    for k in DEFAULT_STRUCTURE:
        if k in body:
            cfg[k] = body[k]
    await set_setting(db, "structure", cfg)
    return cfg


@router.post("/structure/recompute")
async def structure_recompute():
    """On-demand recompute of the persistent map (#61). Runs in its own session
    with an isolated adapter — zero impact on the execution path."""
    return {"recomputed": await struct_map.recompute_all()}


@router.get("/structure/map")
async def structure_map_view(symbol: str = "XAUUSD", db: AsyncSession = Depends(get_db)):
    """The active (current) structure/magnet map: per-TF structure + magnet zones."""
    m = await struct_map.active_map(db, symbol)
    if not m:
        return {"symbol": symbol, "version_id": None, "structures": {}, "zones": []}
    return {
        "symbol": symbol, "version_id": m["version_id"],
        "structures": {tf: {
            "label": s.label,
            "premium_discount": float(s.premium_discount) if s.premium_discount is not None else None,
            "atr": float(s.atr) if s.atr is not None else None,
            "swings": s.swings, "n_levels": len(m["levels_by_tf"].get(tf, [])),
        } for tf, s in m["structures"].items()},
        "zones": [{
            "rank": z.rank, "band": [float(z.price_low), float(z.price_high)],
            "mid": float(z.mid), "score": float(z.score),
            "n_timeframes": z.n_timeframes, "members": z.members,
        } for z in m["zones"]],
    }


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
