from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core import notifications as notif
from beacon_core.crypto import encrypt, has_key
from beacon_core.db.models import Signal, Trade
from beacon_core.notifications.context import build_ctx
from beacon_core.settings_store import get_setting, set_setting
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/notifications", tags=["notifications"],
                   dependencies=[Depends(require_token)])


@router.get("/catalog")
async def get_catalog():
    """Channels + event-type metadata that drives the config UI (also carries the
    template field descriptor + sample object for the message editor)."""
    return notif.catalog()


@router.get("/fields")
async def get_fields():
    """The template field descriptor + a sample object for the live preview.
    Derived from the same registry the renderer resolves, so the picker can never
    advertise a token the renderer can't resolve."""
    return {"fields": notif.field_descriptor(), "sample": notif.sample_ctx()}


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

    if "templates" in incoming:
        # Replace wholesale (the editor sends the full set); sanitize_config drops
        # unknown events and empty subject/body below.
        stored["templates"] = incoming.get("templates") or {}

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


@router.post("/test-event")
async def test_event(body: dict, db: AsyncSession = Depends(get_db)):
    """Fire a real, template-rendered notification for manual testing (#139).

    Body (all optional except `event_id`):
      - `event_id`  : which event to render (required; e.g. "trade_closed").
      - `trade_id`  : build the ctx from this real trade (joins signal/source/
                      account/AI). Omit both ids to render the deterministic
                      sample object.
      - `signal_id` : build the ctx from this signal instead of a trade.
      - `channel_id`: deliver to this one channel regardless of routing; omit to
                      deliver to the event's routed channels.
      - `send`      : must be true to actually deliver. Default false = dry-run
                      that returns the rendered subject/body + ctx and sends
                      nothing (so a bare call never spams a channel).
    """
    body = body or {}
    event_id = body.get("event_id")
    if event_id not in notif.EVENT_IDS:
        raise HTTPException(404, f"unknown event '{event_id}'")

    trade = signal = None
    if body.get("trade_id") is not None:
        trade = await db.get(Trade, int(body["trade_id"]))
        if trade is None:
            raise HTTPException(404, f"trade {body['trade_id']} not found")
    if body.get("signal_id") is not None:
        signal = await db.get(Signal, int(body["signal_id"]))
        if signal is None:
            raise HTTPException(404, f"signal {body['signal_id']} not found")

    if trade is None and signal is None:
        ctx = notif.sample_ctx(event_id)          # no id -> deterministic sample
    else:
        ctx = await build_ctx(db, event_id, trade=trade, signal=signal)

    subject, text = await notif.render_event(db, event_id, ctx)
    out = {"event": event_id, "subject": subject, "body": text, "ctx": ctx, "sent": False}
    if not body.get("send"):
        return out                                # dry-run preview (safe default)

    channel_id = body.get("channel_id")
    if channel_id:
        if channel_id not in notif.CHANNEL_IDS:
            raise HTTPException(404, "unknown channel")
        out["results"] = {channel_id: await notif.send_event_to_channel(
            db, event_id, ctx, channel_id)}
    else:
        res = await notif.notify(db, event_id, ctx)
        out["results"] = res.get("results", {})
    out["sent"] = True
    return out
