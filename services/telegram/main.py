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
import datetime as dt
import hashlib
import re

from sqlalchemy import select
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from beacon_core.ai import service as ai_service
from beacon_core.bus import Bus
from beacon_core.config import CH_SIGNAL_VALID, CH_TG_CONTROL, get_settings
from beacon_core.logging import get_logger
from beacon_core.health import run_health_server
from beacon_core.db.base import Session, init_models
from beacon_core.db.models import Signal, Source, TelegramMessage
from beacon_core.parsing import parse
from beacon_core.execution.planner import validate_signal

log = get_logger("telegram")
settings = get_settings()
bus = Bus()

DEDUPE_WINDOW_MIN = 10
BACKFILL_LIMIT = 200


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().upper())


def _hash(channel: str, text: str) -> str:
    return hashlib.sha256(f"{channel}:{_norm(text)}".encode()).hexdigest()


async def _telegram_sources():
    async with Session()() as s:
        rows = (await s.execute(select(Source).where(Source.kind == "telegram"))).scalars().all()
        return {str(r.external_id): r.id for r in rows if r.external_id}


async def _already_stored(session, chat_key: str, message_id) -> bool:
    if message_id is None:
        return False
    return (await session.execute(select(TelegramMessage.id).where(
        TelegramMessage.chat_id == chat_key,
        TelegramMessage.message_id == int(message_id)))).first() is not None


_BG_TASKS: set = set()


def _validate_bg(signal_id, source_id):
    """Background (non-blocking) signal validation: record the AI's opinion for
    the reconciler/analysis without waiting for it or mutating the traded signal."""
    async def _run():
        try:
            async with Session()() as s2:
                sig2 = await s2.get(Signal, signal_id)
                src2 = await s2.get(Source, source_id) if source_id else None
                if sig2 is not None:
                    await ai_service.apply_signal_validation(s2, sig2, src2, record_only=True)
                    await s2.commit()
        except Exception as exc:                     # never affect ingestion
            log.debug("background validation failed (signal %s): %s", signal_id, exc)
    t = asyncio.create_task(_run())
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)


async def _handle_message(chat_key, source_id, message_id, sender, text, msg_date,
                          is_live: bool = True, reply_to=None):
    """Persist one message; if it parses to a valid signal, store it.

    Only LIVE messages are published to the executor and AI-validated. Historical
    backfill is persisted for the message/signal history but never traded or sent
    to the AI — otherwise a restart would replay old signals as fresh trades and
    hammer the broker's session rate limit.
    """
    ai_rejected = False
    cfg = None
    async with Session()() as s:
        if await _already_stored(s, chat_key, message_id):
            return

        parsed = parse(text or "")
        is_signal = parsed is not None
        parse_status = "none"
        reject_reason = None
        signal_id = None

        if parsed is not None:
            ok, reason = validate_signal(parsed)
            parse_status = "parsed" if ok else "rejected"
            reject_reason = None if ok else reason
            dedupe = _hash(chat_key, text)
            recent = (await s.execute(select(Signal).where(
                Signal.dedupe_hash == dedupe,
                Signal.created_at >= dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(minutes=DEDUPE_WINDOW_MIN)))).scalars().first()
            if recent:
                signal_id = recent.id
                parse_status = "duplicate"
            else:
                # Historical (backfilled) signals are recorded as "history" so
                # they are visible per channel but clearly never queued to trade.
                status = ("validated" if ok else "rejected") if is_live else (
                    "history" if ok else "rejected")
                sig = Signal(
                    source_id=source_id, symbol=parsed.symbol, direction=parsed.direction,
                    entry_from=parsed.entry_from, entry_to=parsed.entry_to, sl=parsed.sl,
                    tps=[str(t) for t in parsed.tps],
                    order_type=parsed.order_type_hint or "MARKET",
                    status=status,
                    reject_reason=reject_reason, raw_text=text, dedupe_hash=dedupe,
                )
                s.add(sig)
                await s.flush()
                signal_id = sig.id

        msg = TelegramMessage(
            source_id=source_id, chat_id=chat_key,
            message_id=int(message_id) if message_id is not None else None,
            sender=sender, text=text, is_signal=is_signal,
            parse_status=parse_status, reject_reason=reject_reason,
            signal_id=signal_id, message_date=msg_date,
            reply_to_message_id=int(reply_to) if reply_to is not None else None,
        )
        s.add(msg)

        # Telegram signals are free-text: AI validates AND corrects them before
        # they can trade (the parser can misread levels). Only freshly-stored,
        # valid, LIVE signals. If the AI is unavailable the signal is executed on
        # the parser output but flagged (fail-open); if the AI rejects it, it is
        # not published.
        new_valid = (parse_status == "parsed" and signal_id is not None)
        if new_valid and is_live:
            cfg = await ai_service.load_config(s)
            # Only BLOCK mode waits for the AI (and can correct/reject) before the
            # signal is published. background/off don't hold up the order.
            if cfg.validation_mode == "block":
                sig_row = await s.get(Signal, signal_id)
                source = await s.get(Source, source_id) if source_id else None
                try:
                    status = await ai_service.apply_signal_validation(s, sig_row, source, cfg=cfg)
                    if status == "rejected":
                        ai_rejected = True
                except Exception as exc:                 # never block ingestion
                    log.warning("AI signal validation failed: %s", exc)

        await s.commit()

    # Only LIVE signals are handed to the executor. Backfilled history is not.
    if is_live and parse_status == "parsed" and signal_id is not None and not ai_rejected:
        await bus.publish(CH_SIGNAL_VALID, {"signal_id": signal_id})
        log.info("signal %s published (%s %s)", signal_id, parsed.direction, parsed.symbol)
        # Non-blocking mode: fire the AI afterwards, for the record only.
        if cfg is not None and cfg.validation_mode == "background":
            _validate_bg(signal_id, source_id)
    elif ai_rejected:
        log.info("signal %s rejected by AI validation", signal_id)
    elif parse_status == "rejected":
        log.info("signal rejected: %s", reject_reason)


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
    """Listen for backfill/reload requests from the API."""
    async for msg in bus.subscribe(CH_TG_CONTROL):
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

    asyncio.create_task(run_health_server("telegram", bus, port=8080))
    await client.start()
    log.info("telegram listening on %s channels", len(watch))
    # Backfill recent history so the portal isn't empty, then stay responsive
    # to on-demand sync requests from the API.
    asyncio.create_task(_backfill(client, sources))
    asyncio.create_task(_control_loop(client, sources))
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
