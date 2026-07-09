from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.execution.guard import DEFAULT_RISK_LIMITS
from beacon_core.settings_store import get_setting, set_setting
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/risk-limits", tags=["risk-limits"],
                   dependencies=[Depends(require_token)])

_FLOATS = ("daily_loss_limit", "per_signal_max_pct_of_daily",
           "max_open_risk_per_account", "max_open_risk_per_symbol")


def _sanitize(cfg: dict | None) -> dict:
    cfg = cfg or {}
    out = dict(DEFAULT_RISK_LIMITS)
    out["enabled"] = bool(cfg.get("enabled", out["enabled"]))
    out["trading_halted"] = bool(cfg.get("trading_halted", False))
    for k in _FLOATS:
        try:
            out[k] = float(cfg.get(k, out[k]))
        except (TypeError, ValueError):
            pass
    return out


@router.get("/config")
async def get_config(db: AsyncSession = Depends(get_db)):
    stored = await get_setting(db, "risk_limits", None)
    out = _sanitize(stored)
    out["configured"] = stored is not None       # False -> Dashboard shows the fail-safe banner
    return out


@router.put("/config")
async def put_config(body: dict, db: AsyncSession = Depends(get_db)):
    clean = _sanitize(body)
    await set_setting(db, "risk_limits", clean)
    out = dict(clean)
    out["configured"] = True
    return out
