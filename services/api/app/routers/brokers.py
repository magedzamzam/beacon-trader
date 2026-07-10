from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.db.models import Broker
from beacon_core.brokers import make_adapter
from beacon_core.crypto import encrypt, has_key
from ..deps import get_db
from ..auth import require_token
from ..schemas import BrokerIn

router = APIRouter(prefix="/brokers", tags=["brokers"], dependencies=[Depends(require_token)])


def _cred_mode(ref: dict) -> str:
    """How this broker's secrets are stored, for UI display (never the values)."""
    ref = ref or {}
    if any(k.endswith("_enc") for k in ref):
        return "encrypted"
    if any(k.endswith("_env") for k in ref):
        return "env"
    return "none"


def _build_credentials_ref(body: BrokerIn) -> dict:
    """If the UI passed raw secrets, store them encrypted (*_enc). Otherwise use
    the provided credentials_ref (env-var references) as-is."""
    if body.api_key or body.username or body.password:
        if not has_key():
            raise HTTPException(400, "SECRET_KEY is not set; cannot store secrets encrypted")
        ref = {"is_demo": body.is_demo}
        if body.api_key:
            ref["api_key_enc"] = encrypt(body.api_key)
        if body.username:
            ref["account_username_enc"] = encrypt(body.username)
        if body.password:
            ref["account_password_enc"] = encrypt(body.password)
        return ref
    return body.credentials_ref or {}


@router.get("")
async def list_brokers(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Broker))).scalars().all()
    return [{"id": b.id, "type": b.type, "name": b.name, "is_demo": b.is_demo,
             "enabled": b.enabled, "cred_mode": _cred_mode(b.credentials_ref)}
            for b in rows]


@router.post("")
async def create_broker(body: BrokerIn, db: AsyncSession = Depends(get_db)):
    b = Broker(type=body.type, name=body.name, is_demo=body.is_demo,
               enabled=body.enabled, credentials_ref=_build_credentials_ref(body))
    db.add(b); await db.commit()
    return {"id": b.id}


@router.get("/{broker_id}/health")
async def broker_health(broker_id: int, db: AsyncSession = Depends(get_db)):
    b = await db.get(Broker, broker_id)
    if not b:
        raise HTTPException(404, "broker not found")
    adapter = make_adapter(b)
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
    adapter = make_adapter(b)
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
    # Allow replacing credentials with UI-entered secrets, stored encrypted —
    # this is how you move Capital.com creds out of .env onto the broker.
    if any(body.get(k) for k in ("api_key", "username", "password")):
        if not has_key():
            raise HTTPException(400, "SECRET_KEY is not set; cannot store secrets encrypted")
        ref = dict(b.credentials_ref or {})
        # drop any prior env/enc references so the two schemes don't mix
        ref = {k: v for k, v in ref.items() if not (k.endswith("_env") or k.endswith("_enc"))}
        ref["is_demo"] = b.is_demo
        if body.get("api_key"):
            ref["api_key_enc"] = encrypt(body["api_key"])
        if body.get("username"):
            ref["account_username_enc"] = encrypt(body["username"])
        if body.get("password"):
            ref["account_password_enc"] = encrypt(body["password"])
        b.credentials_ref = ref
    await db.commit()
    return {"ok": True}


@router.delete("/{broker_id}")
async def delete_broker(broker_id: int, db: AsyncSession = Depends(get_db)):
    b = await db.get(Broker, broker_id)
    if b:
        await db.delete(b); await db.commit()
    return {"ok": True}
