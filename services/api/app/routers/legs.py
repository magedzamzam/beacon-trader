from decimal import Decimal
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.db.models import Account, Event, Leg, Trade
from beacon_core.brokers import build_adapter
from beacon_core.brokers.types import ModifyPositionRequest
from beacon_core.timeutil import utcnow
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/legs", tags=["legs"], dependencies=[Depends(require_token)])
OPEN = ("open", "working", "pending")


async def _adapter_for_account(db, account_id):
    acct = await db.get(Account, account_id)      # act on the mapped account
    _, adapter = await build_adapter(db, acct)
    return adapter


async def _trade_account(db, leg):
    trade = await db.get(Trade, leg.trade_id)
    return trade, trade.account_id


async def _do_close(adapter, leg, db, trade):
    res = await adapter.close_position(leg.broker_position_ref)
    leg.status = "closed"; leg.outcome = "manual"; leg.closed_at = utcnow()
    if res and res.close_price is not None: leg.close_price = res.close_price
    if res and res.realized_pl is not None: leg.realized_pl = res.realized_pl
    db.add(Event(trade_id=trade.id, leg_id=leg.id, kind="closed_by_user", payload={}))


async def _do_cancel(adapter, leg, db, trade):
    await adapter.cancel_order(leg.broker_order_ref)
    leg.status = "cancelled"; leg.outcome = "manual"; leg.closed_at = utcnow()
    db.add(Event(trade_id=trade.id, leg_id=leg.id, kind="cancelled_by_user", payload={}))


async def _do_move_sl(adapter, leg, db, trade, sl: Decimal):
    await adapter.modify_position(ModifyPositionRequest(
        broker_position_ref=leg.broker_position_ref, stop_loss=sl))
    leg.sl = sl; leg.sl_moved = True
    db.add(Event(trade_id=trade.id, leg_id=leg.id, kind="sl_moved_by_user",
                 payload={"new_sl": str(sl)}))


# ---- single-leg ----
@router.post("/{leg_id}/cancel")
async def cancel_leg(leg_id: int, db: AsyncSession = Depends(get_db)):
    leg = await db.get(Leg, leg_id)
    if not leg: raise HTTPException(404, "leg not found")
    if not leg.broker_order_ref or leg.status not in ("working", "pending"):
        raise HTTPException(409, f"leg is {leg.status}; nothing to cancel")
    trade, acct_id = await _trade_account(db, leg)
    adapter = await _adapter_for_account(db, acct_id)
    try: await _do_cancel(adapter, leg, db, trade)
    except HTTPException: raise
    except Exception as exc: raise HTTPException(502, f"broker cancel failed: {exc}")
    finally: await adapter.aclose()
    await db.commit(); return {"ok": True}


@router.post("/{leg_id}/close")
async def close_leg(leg_id: int, db: AsyncSession = Depends(get_db)):
    leg = await db.get(Leg, leg_id)
    if not leg: raise HTTPException(404, "leg not found")
    if not leg.broker_position_ref or leg.status != "open":
        raise HTTPException(409, f"leg is {leg.status}; nothing to close")
    trade, acct_id = await _trade_account(db, leg)
    adapter = await _adapter_for_account(db, acct_id)
    try: await _do_close(adapter, leg, db, trade)
    except Exception as exc: raise HTTPException(502, f"broker close failed: {exc}")
    finally: await adapter.aclose()
    await db.commit(); return {"ok": True}


# ---- bulk ----
class BulkIn(BaseModel):
    action: str                 # close | cancel | move_sl
    leg_ids: Optional[List[int]] = None   # if omitted -> all open legs
    trade_id: Optional[int] = None        # scope to one trade
    sl: Optional[float] = None            # required for move_sl


@router.post("/bulk")
async def bulk(body: BulkIn, db: AsyncSession = Depends(get_db)):
    q = select(Leg).where(Leg.status.in_(OPEN))
    if body.leg_ids:
        q = q.where(Leg.id.in_(body.leg_ids))
    if body.trade_id:
        q = q.where(Leg.trade_id == body.trade_id)
    legs = (await db.execute(q)).scalars().all()
    if not legs:
        return {"ok": True, "affected": 0}

    # group by account so we reuse one broker session per account
    by_acct: dict = {}
    trades: dict = {}
    for leg in legs:
        if leg.trade_id not in trades:
            trades[leg.trade_id] = await db.get(Trade, leg.trade_id)
        by_acct.setdefault(trades[leg.trade_id].account_id, []).append(leg)

    affected, errors = 0, []
    sl = Decimal(str(body.sl)) if body.sl is not None else None
    for acct_id, acct_legs in by_acct.items():
        adapter = await _adapter_for_account(db, acct_id)
        try:
            for leg in acct_legs:
                trade = trades[leg.trade_id]
                try:
                    if body.action == "close" and leg.status == "open" and leg.broker_position_ref:
                        await _do_close(adapter, leg, db, trade); affected += 1
                    elif body.action == "cancel" and leg.status in ("working", "pending") and leg.broker_order_ref:
                        await _do_cancel(adapter, leg, db, trade); affected += 1
                    elif body.action == "move_sl" and leg.status == "open" and leg.broker_position_ref and sl is not None:
                        await _do_move_sl(adapter, leg, db, trade, sl); affected += 1
                except Exception as exc:
                    errors.append(f"leg {leg.id}: {exc}")
        finally:
            await adapter.aclose()
    await db.commit()
    return {"ok": True, "affected": affected, "errors": errors}
