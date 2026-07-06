from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core import notifications as notif
from beacon_core.crypto import encrypt, has_key
from beacon_core.settings_store import get_setting, set_setting
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/notifications", tags=["notifications"],
                   dependencies=[Depends(require_token)])


@router.get("/catalog")
async def get_catalog():
    """Channels + event-type metadata that drives the config UI."""
    return notif.catalog()


@router.get("/config")
async def get_config(db: AsyncSession = Depends(get_db)):
    stored = await get_setting(db, notif.SETTING_KEY, {})
    out = notif.public_config(stored)
    out["has_secret_key"] = has_key()
    return out


@router.put("/config")
async def put_config(body: dict, db: AsyncSession = Depends(get_db)):
    """Merge the incoming config. Secret fields are encrypted only when a new
    plaintext value is supplied; an empty/missing secret keeps the stored one."""
    stored = notif.sanitize_config(await get_setting(db, notif.SETTING_KEY, {}))
    incoming = body or {}
    in_channels = incoming.get("channels") or {}

    for ch in notif.CHANNELS:
        dst = stored["channels"][ch["id"]]
        src = in_channels.get(ch["id"]) or {}
        if "enabled" in src:
            dst["enabled"] = bool(src["enabled"])
        for f in ch["fields"]:
            name = f["name"]
            if f["secret"]:
                val = src.get(name)
                if val:                       # new secret supplied -> encrypt
                    if not has_key():
                        raise HTTPException(
                            400, "SECRET_KEY is not set; cannot store secrets encrypted")
                    dst[name + "_enc"] = encrypt(str(val))
                elif src.get(f"clear_{name}"):  # explicit clear
                    dst.pop(name + "_enc", None)
            elif name in src:
                dst[name] = src[name]

    if "routing" in incoming:
        routing = incoming.get("routing") or {}
        stored["routing"] = {
            e: [c for c in (routing.get(e) or []) if c in notif.CHANNEL_IDS]
            for e in notif.EVENT_IDS}

    clean = notif.sanitize_config(stored)
    await set_setting(db, notif.SETTING_KEY, clean)
    out = notif.public_config(clean)
    out["has_secret_key"] = has_key()
    return out


@router.post("/test/{channel_id}")
async def test_channel(channel_id: str, db: AsyncSession = Depends(get_db)):
    """Send a one-off test message to a channel using its SAVED config."""
    if channel_id not in notif.CHANNEL_IDS:
        raise HTTPException(404, "unknown channel")
    res = await notif.send_test(db, channel_id)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "send failed")
    return res
