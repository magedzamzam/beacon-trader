"""Entry/planner config API (#67): the market-on-receipt chase guard + related
entry settings. GET/PUT the `planner` setting so entry behaviour is edited from
the platform, never hardcoded."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.execution.planner import DEFAULT_PLANNER
from beacon_core.settings_store import get_setting, set_setting
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/planner", tags=["planner"],
                   dependencies=[Depends(require_token)])

_FLOATS = ("chase_tolerance_r", "chase_tolerance_atr", "max_tp_distance_pct")


def _sanitize(cfg: dict | None) -> dict:
    cfg = cfg or {}
    out = dict(DEFAULT_PLANNER)
    out["honor_market_hint"] = bool(cfg.get("honor_market_hint", out["honor_market_hint"]))
    out["beyond_tolerance"] = "skip" if cfg.get("beyond_tolerance") == "skip" else "limit"
    for k in _FLOATS:
        try:
            out[k] = max(0.0, float(cfg.get(k, out[k])))
        except (TypeError, ValueError):
            pass
    return out


@router.get("/config")
async def get_config(db: AsyncSession = Depends(get_db)):
    stored = await get_setting(db, "planner", None)
    out = _sanitize(stored)
    out["configured"] = stored is not None
    return out


@router.put("/config")
async def put_config(body: dict, db: AsyncSession = Depends(get_db)):
    clean = _sanitize(body)
    await set_setting(db, "planner", clean)
    return {**clean, "configured": True}
