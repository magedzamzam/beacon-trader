from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.execution.guard import DEFAULT_RISK_LIMITS, risk_limit_reason
from beacon_core.db.models import Account, Trade
from beacon_core.settings_store import get_setting, set_setting
from beacon_core.timeutil import utcnow
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


@router.get("/status")
async def status(account_id: int | None = None, db: AsyncSession = Depends(get_db)):
    """Live risk state so the UI can show WHY trading is (or isn't) blocked, not
    just the logs (#65). Applies the exact same `risk_limit_reason` the executor
    uses (planned_risk=0 -> only the kill-switch + daily-loss floor can fire), per
    account, off each account's realized P&L since UTC midnight."""
    stored = await get_setting(db, "risk_limits", None)
    cfg = _sanitize(stored) if stored is not None else dict(DEFAULT_RISK_LIMITS)
    floor = abs(float(cfg.get("daily_loss_limit") or 0))

    day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    q = (select(Account.id, Account.name,
                func.coalesce(func.sum(Trade.realized_pl), 0))
         .outerjoin(Trade, (Trade.account_id == Account.id)
                    & (Trade.created_at >= day_start))
         .where(Account.enabled == True).group_by(Account.id, Account.name))
    if account_id is not None:
        q = q.where(Account.id == account_id)
    rows = (await db.execute(q)).all()

    accounts = []
    any_blocked = False
    for aid, name, day_realized in rows:
        dr = float(day_realized or 0)
        reason = risk_limit_reason(planned_risk=0, day_realized=dr,
                                   open_risk_symbol=0, open_risk_account=0, cfg=cfg)
        blocked = reason is not None
        any_blocked = any_blocked or blocked
        accounts.append({"account_id": aid, "name": name, "day_realized": round(dr, 2),
                         "floor": -floor if floor > 0 else None,
                         "blocked": blocked, "reason": reason})

    return {"configured": stored is not None, "enabled": cfg["enabled"],
            "trading_halted": cfg["trading_halted"], "daily_loss_limit": floor,
            "blocked": any_blocked, "accounts": accounts}
