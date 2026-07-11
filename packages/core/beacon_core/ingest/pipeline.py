"""The single ingest pipeline (#35): parse -> validate -> dedupe -> persist
Signal (+ optional channel message row) -> AI-validate free-text -> publish.

Before this, the Telegram listener and the HTTP/webhook router each had their
own copy — with DIFFERENT dedupe keys and two Signal builders to keep in sync.
Everything funnels through `ingest_message` now; one dedupe strategy, one
Signal construction, one place to change."""
from __future__ import annotations

import datetime as dt
import hashlib

from sqlalchemy import select

from ..ai import service as ai_service
from ..bus import Bus
from ..config import CH_SIGNAL_VALID
from ..db.base import Session
from ..db.models import Signal, Source, TelegramMessage
from ..logging import get_logger
from ..parsing import parse
from ..execution.planner import validate_signal
from ..tasks import spawn_bg
from ..timeutil import utcnow
from .base import InboundMessage, IngestResult

bus = Bus()
log = get_logger("ingest")

DEDUPE_WINDOW_MIN = 10


def _signal_hash(source_id, symbol, direction, entry_from, sl) -> str:
    """One dedupe key across every channel: the structured signal identity. A
    reworded free-text repost of the same trade collapses to this too (unlike
    the old raw-text hash)."""
    key = f"{source_id}:{symbol}:{direction}:{entry_from}:{sl}"
    return hashlib.sha256(key.encode()).hexdigest()


async def _already_stored(session, chat_key, message_id) -> bool:
    if message_id is None or chat_key is None:
        return False
    return (await session.execute(select(TelegramMessage.id).where(
        TelegramMessage.chat_id == chat_key,
        TelegramMessage.message_id == int(message_id)))).first() is not None


def _signal_status(ok: bool, is_live: bool) -> str:
    # Historical (backfilled) valid signals are 'history' — visible per channel
    # but never queued to trade; live valid ones are 'validated'.
    if is_live:
        return "validated" if ok else "rejected"
    return "history" if ok else "rejected"


def _validate_bg(signal_id, source_id) -> None:
    """Record the AI's opinion in the background (record_only) without holding up
    or mutating the traded signal — used in 'background' validation mode."""
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
    spawn_bg(_run())


async def ingest_message(session, msg: InboundMessage) -> IngestResult:
    """Run one inbound message through the whole pipeline. Commits `session`."""
    # Message-level idempotency (backfill re-run): skip a message we already saw.
    if msg.persist_message and await _already_stored(session, msg.chat_key, msg.message_id):
        return IngestResult(None, "seen", False, None, False)

    parsed = msg.parsed if msg.parsed is not None else (parse(msg.text or "") if msg.text else None)
    is_signal = parsed is not None
    signal_id = None
    parse_status = "none"
    reject_reason = None
    ok = False
    ai_rejected = False
    cfg = None

    if parsed is not None:
        ok, reason = validate_signal(parsed)
        reject_reason = None if ok else reason
        dedupe = _signal_hash(msg.source_id, parsed.symbol, parsed.direction,
                              parsed.entry_from, parsed.sl)
        recent = (await session.execute(select(Signal).where(
            Signal.dedupe_hash == dedupe,
            Signal.created_at >= utcnow() - dt.timedelta(minutes=DEDUPE_WINDOW_MIN)
        ))).scalars().first()
        if recent:
            signal_id = recent.id
            parse_status = "duplicate"
        else:
            sig = Signal(
                source_id=msg.source_id, symbol=parsed.symbol, direction=parsed.direction,
                entry_from=parsed.entry_from, entry_to=parsed.entry_to, sl=parsed.sl,
                tps=[str(t) for t in parsed.tps],
                order_type=parsed.order_type_hint or "MARKET",
                status=_signal_status(ok, msg.is_live),
                reject_reason=reject_reason,
                raw_text=msg.text or (parsed.raw_text or ""), dedupe_hash=dedupe,
            )
            session.add(sig)
            await session.flush()
            signal_id = sig.id
            parse_status = "parsed" if ok else "rejected"

            # Free-text signals are AI-validated/corrected before they can trade
            # (the parser can misread levels). Only BLOCK mode waits here.
            if ok and msg.is_live and msg.from_freetext:
                cfg = await ai_service.load_config(session)
                if cfg.validation_mode == "block":
                    source = await session.get(Source, msg.source_id) if msg.source_id else None
                    try:
                        _status = await ai_service.apply_signal_validation(session, sig, source, cfg=cfg)
                        if _status == "rejected":
                            ai_rejected = True
                            ok = False
                            reject_reason = sig.reject_reason or "AI rejected the signal"
                    except Exception as exc:             # never block ingestion
                        log.warning("AI signal validation failed: %s", exc)

    if msg.persist_message:
        session.add(TelegramMessage(
            source_id=msg.source_id, chat_id=msg.chat_key,
            message_id=int(msg.message_id) if msg.message_id is not None else None,
            sender=msg.sender, text=msg.text, is_signal=is_signal,
            parse_status=parse_status, reject_reason=reject_reason,
            signal_id=signal_id, message_date=msg.msg_date,
            reply_to_message_id=int(msg.reply_to) if msg.reply_to is not None else None,
        ))

    await session.commit()

    # Only fresh, valid, LIVE signals reach the executor (durable queue).
    published = False
    if msg.is_live and parse_status == "parsed" and signal_id is not None and not ai_rejected:
        await bus.enqueue(CH_SIGNAL_VALID, {"signal_id": signal_id})
        published = True
        log.info("signal %s published (%s %s)", signal_id, parsed.direction, parsed.symbol)
        if msg.from_freetext and cfg is not None and cfg.validation_mode == "background":
            _validate_bg(signal_id, msg.source_id)
    elif ai_rejected:
        log.info("signal %s rejected by AI validation", signal_id)
    elif parse_status == "rejected":
        log.info("signal rejected: %s", reject_reason)

    if parse_status == "duplicate":
        return IngestResult(signal_id, "duplicate", False, "duplicate", False)
    return IngestResult(signal_id, parse_status, ok and not ai_rejected, reject_reason, published)
