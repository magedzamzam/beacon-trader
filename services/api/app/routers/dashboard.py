from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.db.models import Leg, Trade
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/dashboard", tags=["dashboard"],
                   dependencies=[Depends(require_token)])


@router.get("/summary")
async def dashboard_summary(account_id: int | None = None,
                            db: AsyncSession = Depends(get_db)):
    """Headline KPIs. When account_id is given, everything is scoped to that
    account (Leg counts join through Trade to resolve the account)."""
    def scope_trade(q):
        return q.where(Trade.account_id == account_id) if account_id is not None else q

    def scope_leg(q):
        # Legs carry no account_id of their own — resolve it via their trade.
        if account_id is not None:
            return q.join(Trade, Trade.id == Leg.trade_id).where(Trade.account_id == account_id)
        return q

    total_trades = (await db.execute(scope_trade(select(func.count(Trade.id))))).scalar() or 0
    open_trades = (await db.execute(scope_trade(select(func.count(Trade.id)).where(
        Trade.status.in_(("open", "partial")))))).scalar() or 0
    total_pl = float((await db.execute(scope_trade(select(func.coalesce(
        func.sum(Trade.realized_pl), 0))))).scalar() or 0)
    open_legs = (await db.execute(scope_leg(select(func.count(Leg.id)).where(
        Leg.status.in_(("open", "working", "pending")))))).scalar() or 0
    closed = (await db.execute(scope_leg(select(func.count(Leg.id)).where(
        Leg.status == "closed")))).scalar() or 0
    wins = (await db.execute(scope_leg(select(func.count(Leg.id)).where(
        Leg.outcome == "tp_hit")))).scalar() or 0
    win_rate = (wins / closed * 100.0) if closed else 0.0
    return {"total_pl": round(total_pl, 2), "total_trades": total_trades,
            "open_trades": open_trades, "open_legs": open_legs,
            "win_rate": round(win_rate, 2)}
