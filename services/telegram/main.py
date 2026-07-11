"""Telegram detection + full-history persistence + parse + validate + publish.

Listens to the channels configured as telegram Sources. EVERY message on a
watched channel is persisted (signal or not) so the portal shows the complete
history per channel. Messages that parse into a valid signal are stored as a
Signal, published for the executor, and (when enabled) assessed by the AI layer.

On startup — and on demand via the `telegram.control` Redis channel — it
backfills recent channel history so a freshly-installed portal isn't empty.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from beacon_core.bus import Bus
from beacon_core.config import CH_TG_CONTROL, get_settings
from beacon_core.logging import get_logger
from beacon_core.health import run_health_server
from beacon_core.db.base import Session, init_models
from beacon_core.db.models import Source
from beacon_core.ingest import (BaseInboundChannel, InboundMessage,
                                ingest_message, register_channel)
from beacon_core.tasks import spawn_bg

log = get_logger("telegram")
settings = get_settings()
bus = Bus()

BACKFILL_LIMIT = 200


async def _telegram_sources():
    async with Session()() as s:
        rows = (await s.execute(select(Source).where(Source.kind == "telegram"))).scalars().all()
        return {str(r.external_id): r.id for r in rows if r.external_id}


async def _handle_message(chat_key, source_id, message_id, sender, text, msg_date,
                          is_live: bool = True, reply_to=None):
    """Normalize one Telegram message and run it through the shared ingest
    pipeline (#35): persist the message + (if it parses to a valid signal) store
    and publish it. Telegram is free-text, so the pipeline AI-validates/corrects
    before it can trade; only LIVE messages are published and AI-checked."""
    async with Session()() as s:
        await ingest_message(s, InboundMessage(
            source_id=source_id, kind="telegram", text=text or "",
            from_freetext=True, is_live=is_live, persist_message=True,
            chat_key=chat_key, message_id=message_id, sender=sender,
            msg_date=msg_date, reply_to=reply_to))


def _sender_name(message) -> str | None:
    try:
        who = getattr(message, "post_author", None)
        if who:
            return str(who)
        sid = getattr(message, "sender_id", None)
        return str(sid) if sid is not None else None
    except Exception:
        return None


async def _backfill(client, sources: dict, limit: int = BACKFILL_LIMIT) -> None:
    """Pull recent history for each watched channel and persist anything new."""
    for chat_key, source_id in sources.items():
        try:
            chat = int(chat_key)
        except ValueError:
            chat = chat_key
        try:
            count = 0
            async for message in client.iter_messages(chat, limit=limit):
                text = getattr(message, "message", None) or ""
                if not text:
                    continue
                await _handle_message(
                    chat_key, source_id, message.id, _sender_name(message), text,
                    getattr(message, "date", None), is_live=False,
                    reply_to=getattr(message, "reply_to_msg_id", None))
                count += 1
            log.info("backfill %s: scanned %s messages", chat_key, count)
        except Exception as exc:
            log.warning("backfill failed for %s: %s", chat_key, exc)


async def _control_loop(client, sources: dict) -> None:
    """Listen for backfill/reload requests from the API (self-heals on drops)."""
    async for msg in bus.subscribe_forever(CH_TG_CONTROL):
        action = (msg or {}).get("action")
        if action == "backfill":
            limit = int((msg or {}).get("limit", BACKFILL_LIMIT))
            log.info("control: backfill requested (limit=%s)", limit)
            await _backfill(client, sources, limit=limit)


async def main() -> None:
    await init_models()
    if not (settings.tg_api_id and settings.tg_api_hash and settings.tg_session):
        log.error("TG_API_ID / TG_API_HASH / TG_SESSION not set. Run login.py first.")
        # still serve health so the container is 'up' and reports the problem
        await run_health_server("telegram", bus, port=8080)
        return

    sources = await _telegram_sources()
    watch = [int(k) for k in sources.keys()]
    client = TelegramClient(StringSession(settings.tg_session),
                            int(settings.tg_api_id), settings.tg_api_hash)

    @client.on(events.NewMessage(chats=watch or None))
    async def on_message(event):
        chat_key = str(event.chat_id)
        source_id = sources.get(chat_key)
        if source_id is None:
            return
        try:
            await _handle_message(
                chat_key, source_id, event.message.id, _sender_name(event.message),
                event.message.message or "", getattr(event.message, "date", None),
                reply_to=getattr(event.message, "reply_to_msg_id", None))
        except Exception as exc:
            log.warning("message handling failed: %s", exc)

    spawn_bg(run_health_server("telegram", bus, port=8080))
    await client.start()
    log.info("telegram listening on %s channels", len(watch))
    # Backfill recent history so the portal isn't empty, then stay responsive
    # to on-demand sync requests from the API.
    spawn_bg(_backfill(client, sources))
    spawn_bg(_control_loop(client, sources))
    await client.run_until_disconnected()


class TelegramInboundChannel(BaseInboundChannel):
    """Thin BaseInboundChannel over the Telethon listener (#35): it owns the
    transport, backfill, and control loop; per-message normalization + the
    parse/validate/dedupe/persist/publish pipeline live in _handle_message /
    beacon_core.ingest."""
    kind = "telegram"

    async def run(self) -> None:
        await main()


register_channel("telegram", TelegramInboundChannel)


if __name__ == "__main__":
    asyncio.run(main())
