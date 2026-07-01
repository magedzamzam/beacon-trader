from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.db.models import Account
from ..deps import get_db
from ..auth import require_token
from ..schemas import AccountIn

router = APIRouter(prefix="/accounts", tags=["accounts"], dependencies=[Depends(require_token)])


@router.get("")
async def list_accounts(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Account))).scalars().all()
    return [{"id": a.id, "broker_id": a.broker_id, "name": a.name,
             "broker_account_id": a.broker_account_id, "currency": a.currency,
             "enabled": a.enabled, "risk_config": a.risk_config} for a in rows]


@router.post("")
async def create_account(body: AccountIn, db: AsyncSession = Depends(get_db)):
    a = Account(**body.model_dump()); db.add(a); await db.commit()
    return {"id": a.id}


@router.patch("/{account_id}")
async def update_account(account_id: int, body: dict, db: AsyncSession = Depends(get_db)):
    a = await db.get(Account, account_id)
    for k in ("name", "currency", "enabled", "risk_config"):
        if k in body:
            setattr(a, k, body[k])
    await db.commit()
    return {"ok": True}


@router.delete("/{account_id}")
async def delete_account(account_id: int, db: AsyncSession = Depends(get_db)):
    a = await db.get(Account, account_id)
    if a:
        await db.delete(a); await db.commit()
    return {"ok": True}
