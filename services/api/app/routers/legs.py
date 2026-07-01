import datetime as dt
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.db.models import Account, Broker, Event, Leg, Trade
from beacon_core.brokers import get_adapter, resolve_credentials
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/legs", tags=["legs"], dependencies=[Depends(require_token)])


async def _adapter_for_leg(db, leg):
    trade = await db.get(Trade, leg.trade_id)
    acct = await db.get(Account, trade.account_id)
    broker = await db.get(Broker, acct.broker_id)
    creds = resolve_credentials(broker.credentials_ref); creds.setdefault("is_demo", broker.is_demo)
    return trade, get_adapter(broker.type, creds)


@router.post("/{leg_id}/cancel")
async def cancel_leg(leg_id: int, db: AsyncSession = Depends(get_db)):
    """Cancel a resting order at the broker and mark the leg cancelled."""
    leg = await db.get(Leg, leg_id)
    if not leg:
        raise HTTPException(404, "leg not found")
    if not leg.broker_order_ref or leg.status not in ("working", "pending"):
        raise HTTPException(409, f"leg is {leg.status}; nothing to cancel")
    trade, adapter = await _adapter_for_leg(db, leg)
    try:
        await adapter.cancel_order(leg.broker_order_ref)
    except Exception as exc:
        raise HTTPException(502, f"broker cancel failed: {exc}")
    finally:
        await adapter.aclose()
    leg.status = "cancelled"; leg.outcome = "manual"; leg.closed_at = dt.datetime.now(dt.timezone.utc)
    db.add(Event(trade_id=trade.id, leg_id=leg.id, kind="cancelled_by_user", payload={}))
    await db.commit()
    return {"ok": True}


@router.post("/{leg_id}/close")
async def close_leg(leg_id: int, db: AsyncSession = Depends(get_db)):
    """Close an open position at the broker and mark the leg closed (manual)."""
    leg = await db.get(Leg, leg_id)
    if not leg:
        raise HTTPException(404, "leg not found")
    if not leg.broker_position_ref or leg.status != "open":
        raise HTTPException(409, f"leg is {leg.status}; nothing to close")
    trade, adapter = await _adapter_for_leg(db, leg)
    try:
        res = await adapter.close_position(leg.broker_position_ref)
    except Exception as exc:
        raise HTTPException(502, f"broker close failed: {exc}")
    finally:
        await adapter.aclose()
    leg.status = "closed"; leg.outcome = "manual"
    leg.closed_at = dt.datetime.now(dt.timezone.utc)
    if res and res.close_price is not None:
        leg.close_price = res.close_price
    if res and res.realized_pl is not None:
        leg.realized_pl = res.realized_pl
    db.add(Event(trade_id=trade.id, leg_id=leg.id, kind="closed_by_user",
                 payload={"pl": str(leg.realized_pl) if leg.realized_pl is not None else None}))
    await db.commit()
    return {"ok": True}
