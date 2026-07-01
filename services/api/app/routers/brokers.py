from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.db.models import Broker
from beacon_core.brokers import get_adapter, resolve_credentials
from ..deps import get_db
from ..auth import require_token
from ..schemas import BrokerIn

router = APIRouter(prefix="/brokers", tags=["brokers"], dependencies=[Depends(require_token)])


@router.get("")
async def list_brokers(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Broker))).scalars().all()
    return [{"id": b.id, "type": b.type, "name": b.name, "is_demo": b.is_demo,
             "enabled": b.enabled} for b in rows]


@router.post("")
async def create_broker(body: BrokerIn, db: AsyncSession = Depends(get_db)):
    b = Broker(type=body.type, name=body.name, is_demo=body.is_demo,
               enabled=body.enabled, credentials_ref=body.credentials_ref)
    db.add(b); await db.commit()
    return {"id": b.id}


@router.get("/{broker_id}/health")
async def broker_health(broker_id: int, db: AsyncSession = Depends(get_db)):
    b = await db.get(Broker, broker_id)
    if not b:
        raise HTTPException(404, "broker not found")
    creds = resolve_credentials(b.credentials_ref); creds.setdefault("is_demo", b.is_demo)
    adapter = get_adapter(b.type, creds)
    try:
        return await adapter.healthcheck()
    finally:
        await adapter.aclose()


@router.get("/{broker_id}/accounts")
async def broker_live_accounts(broker_id: int, db: AsyncSession = Depends(get_db)):
    """Fetch the account list live from the broker (for the add-account picker)."""
    b = await db.get(Broker, broker_id)
    if not b:
        raise HTTPException(404, "broker not found")
    creds = resolve_credentials(b.credentials_ref); creds.setdefault("is_demo", b.is_demo)
    adapter = get_adapter(b.type, creds)
    try:
        return await adapter.list_accounts()
    except Exception as exc:
        raise HTTPException(502, f"broker fetch failed: {exc}")
    finally:
        await adapter.aclose()


@router.patch("/{broker_id}")
async def update_broker(broker_id: int, body: dict, db: AsyncSession = Depends(get_db)):
    b = await db.get(Broker, broker_id)
    if not b:
        raise HTTPException(404, "broker not found")
    for k in ("name", "is_demo", "enabled", "credentials_ref"):
        if k in body:
            setattr(b, k, body[k])
    await db.commit()
    return {"ok": True}


@router.delete("/{broker_id}")
async def delete_broker(broker_id: int, db: AsyncSession = Depends(get_db)):
    b = await db.get(Broker, broker_id)
    if b:
        await db.delete(b); await db.commit()
    return {"ok": True}
