"""Entry-filter config API (#48): the trend-alignment (4h EMA200) gate. GET/PUT
the `entry_filters` setting so the filter can be A/B'd and rolled back live."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.execution.trend_filter import DEFAULT_TREND_FILTER, trend_filter_cfg
from beacon_core.ta.registry import TF_RESOLUTION
from beacon_core.settings_store import get_setting, set_setting
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/entry-filters", tags=["entry-filters"],
                   dependencies=[Depends(require_token)])


def _sanitize_trend(cfg: dict | None) -> dict:
    cfg = trend_filter_cfg({"trend_alignment": cfg or {}})   # defaults + known keys
    out = dict(cfg)
    out["enabled"] = bool(cfg.get("enabled"))
    # timeframe must be a known TA resolution, else fall back to the default.
    if out.get("timeframe") not in TF_RESOLUTION:
        out["timeframe"] = DEFAULT_TREND_FILTER["timeframe"]
    try:
        out["ema_period"] = max(2, min(500, int(cfg.get("ema_period", 200))))
    except (TypeError, ValueError):
        out["ema_period"] = DEFAULT_TREND_FILTER["ema_period"]
    out["mode"] = "desize" if cfg.get("mode") == "desize" else "skip"
    try:
        out["desize_factor"] = max(0.0, min(1.0, float(cfg.get("desize_factor", 0.25))))
    except (TypeError, ValueError):
        out["desize_factor"] = DEFAULT_TREND_FILTER["desize_factor"]
    return out


@router.get("/config")
async def get_config(db: AsyncSession = Depends(get_db)):
    stored = await get_setting(db, "entry_filters", None)
    ta = (stored or {}).get("trend_alignment")
    return {"trend_alignment": _sanitize_trend(ta),
            "configured": stored is not None}


@router.put("/config")
async def put_config(body: dict, db: AsyncSession = Depends(get_db)):
    stored = dict(await get_setting(db, "entry_filters", {}) or {})
    stored["trend_alignment"] = _sanitize_trend((body or {}).get("trend_alignment"))
    await set_setting(db, "entry_filters", stored)
    return {"trend_alignment": stored["trend_alignment"], "configured": True}
