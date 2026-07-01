"""Telegram detection + parse + validate + publish.

Listens to the channels configured as telegram Sources, parses each message,
validates geometry, dedupes, persists a Signal, and publishes signal_id on the
validated channel for the executor. Non-signals are ignored silently.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import re

from sqlalchemy import select
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from beacon_core.bus import Bus
from beacon_core.config import CH_SIGNAL_VALID, get_settings
from beacon_core.logging import get_logger
from beacon_core.health import run_health_server
from beacon_core.db.base import Session, init_models
from beacon_core.db.models import Signal, Source
from beacon_core.parsing import parse
from beacon_core.execution.planner import validate_signal

log = get_logger("telegram")
settings = get_settings()
bus = Bus()

DEDUPE_WINDOW_MIN = 10


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().upper())


def _hash(channel: str, text: str) -> str:
    return hashlib.sha256(f"{channel}:{_norm(text)}".encode()).hexdigest()


async def _telegram_sources():
    async with Session()() as s:
        rows = (await s.execute(select(Source).where(Source.kind == "telegram"))).scalars().all()
        return {str(r.external_id): r.id for r in rows if r.external_id}


async def _persist_and_publish(source_id, chat_key, message_text):
    parsed = parse(message_text)
    if parsed is None:
        return
    ok, reason = validate_signal(parsed)
    dedupe = _hash(chat_key, message_text)

    async with Session()() as s:
        # dedupe within window
        recent = (await s.execute(select(Signal).where(
            Signal.dedupe_hash == dedupe,
            Signal.created_at >= dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(minutes=DEDUPE_WINDOW_MIN)))).scalars().first()
        if recent:
            log.info("duplicate signal ignored (%s)", dedupe[:8])
            return

        sig = Signal(
            source_id=source_id, symbol=parsed.symbol, direction=parsed.direction,
            entry_from=parsed.entry_from, entry_to=parsed.entry_to, sl=parsed.sl,
            tps=[str(t) for t in parsed.tps],
            order_type=parsed.order_type_hint or "MARKET",
            status="validated" if ok else "rejected",
            reject_reason=None if ok else reason,
            raw_text=message_text, dedupe_hash=dedupe,
        )
        s.add(sig)
        await s.commit()
        sig_id = sig.id

    if ok:
        await bus.publish(CH_SIGNAL_VALID, {"signal_id": sig_id})
        log.info("signal %s published (%s %s)", sig_id, parsed.direction, parsed.symbol)
    else:
        log.info("signal rejected: %s", reason)


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
            await _persist_and_publish(source_id, chat_key, event.message.message or "")
        except Exception as exc:
            log.warning("message handling failed: %s", exc)

    asyncio.create_task(run_health_server("telegram", bus, port=8080))
    await client.start()
    log.info("telegram listening on %s channels", len(watch))
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
