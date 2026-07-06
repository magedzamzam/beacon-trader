"""Notification dispatch: resolve routing + channel secrets, format a message,
and send. Best-effort — a failing channel never raises to the caller (trading
must never be affected by a notification).
"""
from __future__ import annotations

from typing import Optional

from ..crypto import decrypt
from ..logging import get_logger
from ..settings_store import get_setting
from . import config as C
from .senders import SENDERS

log = get_logger("notifications")

_CHANNEL_BY_ID = {c["id"]: c for c in C.CHANNELS}
EVENT_LABEL = {e["id"]: e["label"] for g in C.EVENT_GROUPS for e in g["events"]}


def resolve_channel(channel_id: str, stored: dict) -> dict:
    """A channel's config with secrets decrypted to plaintext (for a sender)."""
    ch = _CHANNEL_BY_ID.get(channel_id)
    src = (stored.get("channels") or {}).get(channel_id) or {}
    out = {"enabled": bool(src.get("enabled"))}
    if not ch:
        return out
    for f in ch["fields"]:
        name = f["name"]
        if f["secret"]:
            enc = src.get(name + "_enc")
            try:
                out[name] = decrypt(enc) if enc else None
            except Exception:                 # bad/rotated SECRET_KEY
                out[name] = None
        else:
            out[name] = src.get(name, f.get("default"))
    return out


def format_message(event_id: str, ctx: Optional[dict]) -> tuple[str, str]:
    """(subject, text) for an event. `ctx` carries plain values, all optional."""
    ctx = ctx or {}
    label = EVENT_LABEL.get(event_id, event_id)
    sym = ctx.get("symbol") or ""
    subject = f"[Beacon] {label}" + (f" — {sym}" if sym else "")

    lines = []

    def add(k, v):
        if v not in (None, "", []):
            lines.append(f"{k}: {v}")

    add("Symbol", sym)
    add("Direction", ctx.get("direction"))
    add("Entry", ctx.get("entry"))
    add("Price", ctx.get("price"))
    add("TP", ctx.get("tp"))
    add("SL", ctx.get("sl"))
    add("P&L", ctx.get("pl"))
    add("Account", ctx.get("account"))
    add("Source", ctx.get("source"))
    if ctx.get("detail"):
        lines.append(str(ctx["detail"]))
    return subject, ("\n".join(lines) if lines else label)


async def _load(session) -> dict:
    return C.sanitize_config(await get_setting(session, C.SETTING_KEY, {}))


async def notify(session, event_id: str, ctx: Optional[dict] = None) -> dict:
    """Send `event_id` to every enabled, routed channel that has a sender.
    Never raises; returns a per-channel result map for logging/inspection."""
    try:
        stored = await _load(session)
    except Exception as exc:
        log.warning("notify %s: could not load config: %s", event_id, exc)
        return {"event": event_id, "results": {}}

    targets = stored.get("routing", {}).get(event_id) or []
    if not targets:
        return {"event": event_id, "results": {}}

    subject, text = format_message(event_id, ctx)
    results = {}
    for ch_id in targets:
        sender = SENDERS.get(ch_id)
        if sender is None:
            results[ch_id] = "no_sender"
            continue
        cfg = resolve_channel(ch_id, stored)
        if not cfg.get("enabled"):
            results[ch_id] = "disabled"
            continue
        try:
            await sender(cfg, subject, text)
            results[ch_id] = "ok"
        except Exception as exc:
            log.warning("notify %s -> %s failed: %s", event_id, ch_id, exc)
            results[ch_id] = f"error: {exc}"
    if any(v == "ok" for v in results.values()):
        log.info("notify %s -> %s", event_id, results)
    return {"event": event_id, "results": results}


async def send_test(session, channel_id: str) -> dict:
    """Send a one-off test message to a single channel (Send-test button)."""
    sender = SENDERS.get(channel_id)
    if sender is None:
        return {"ok": False, "error": "Delivery for this channel is not built yet."}
    try:
        stored = await _load(session)
    except Exception as exc:
        return {"ok": False, "error": f"config load failed: {exc}"}
    cfg = resolve_channel(channel_id, stored)
    if not cfg.get("enabled"):
        return {"ok": False, "error": "Channel is not enabled — enable and save it first."}
    try:
        await sender(cfg, "[Beacon] Test notification",
                     "This is a test from Beacon Trader. If you can read this, the channel works.")
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
