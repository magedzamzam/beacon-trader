"""Link channel outcome follow-ups to the signal they refer to and persist them
as SignalClaim rows. Incremental (high-water mark on message id) and idempotent
(unique on message_id). Zero impact on the trading path."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .bayes import time_link_confidence
from ..db.models import SignalClaim, TelegramMessage
from ..logging import get_logger
from ..parsing.outcomes import parse_outcome
from ..settings_store import get_setting, set_setting

log = get_logger("reconcile.claims")

_HWM_KEY = "reconcile_hwm"


async def _resolve_signal(session, msg: TelegramMessage, max_hours: int):
    """Resolve an outcome message to its signal, returning (signal_id, confidence).
    Prefer the Telegram reply link (confidence 1.0); else the most recent signal in
    the same chat within `max_hours` before it (confidence decays with the time
    gap). (None, None) when it can't be resolved."""
    if msg.reply_to_message_id:
        parent = (await session.execute(select(TelegramMessage).where(
            TelegramMessage.chat_id == msg.chat_id,
            TelegramMessage.message_id == msg.reply_to_message_id))).scalars().first()
        if parent and parent.signal_id:
            return parent.signal_id, 1.0            # explicit reply -> high confidence

    if msg.message_date is None:
        return None, None
    lo = msg.message_date - dt.timedelta(hours=max_hours)
    parent = (await session.execute(select(TelegramMessage).where(
        TelegramMessage.chat_id == msg.chat_id,
        TelegramMessage.signal_id.isnot(None),
        TelegramMessage.message_date <= msg.message_date,
        TelegramMessage.message_date >= lo)
        .order_by(TelegramMessage.message_date.desc()))).scalars().first()
    if not parent:
        return None, None
    gap_h = (msg.message_date - parent.message_date).total_seconds() / 3600.0 \
        if parent.message_date else float(max_hours)
    return parent.signal_id, time_link_confidence(gap_h, max_hours)


MAX_REPORTED_ERRORS = 20


async def link_claims(session, *, max_hours: int = 12, full: bool = False,
                      cap: int = 20000) -> dict:
    """Process non-signal messages newer than the high-water mark; for each that
    parses as an outcome and resolves to a signal, upsert a SignalClaim.

    ISOLATED PER MESSAGE (#173). The high-water mark used to be written only after
    the whole loop finished, so a single message that raised discarded the entire
    pass — and the next run re-read from the same mark and failed in the same
    place. That wedged linking from 2026-07-27 with 1,574 messages unscanned and
    zero claims for three days, invisibly: both call sites swallowed the exception
    and the only log line was gated on success.

    Each message now gets its own SAVEPOINT (a DB error poisons the transaction,
    so try/except alone would not be enough) and is skipped, logged and counted on
    failure. The mark always advances over everything scanned, so a bad message
    costs one claim instead of every claim after it. Re-run with `full=True` to
    rebuild anything a skip lost — the messages stay in `telegram_messages`."""
    hwm = 0 if full else int(await get_setting(session, _HWM_KEY, 0) or 0)
    msgs = (await session.execute(
        select(TelegramMessage)
        .where(TelegramMessage.is_signal.is_(False), TelegramMessage.id > hwm)
        .order_by(TelegramMessage.id.asc()).limit(cap))).scalars().all()

    added, skipped, max_id = 0, 0, hwm
    errors = []
    for m in msgs:
        max_id = max(max_id, m.id)
        try:
            outcome = parse_outcome(m.text or "")
            if not outcome:
                continue
            async with session.begin_nested():        # SAVEPOINT per message
                sig_id, confidence = await _resolve_signal(session, m, max_hours)
                if sig_id is None:
                    continue
                res = await session.execute(
                    pg_insert(SignalClaim).values(
                        signal_id=sig_id, source_id=m.source_id, message_id=m.id,
                        max_tp_claimed=outcome["max_tp"], sl_claimed=outcome["sl_hit"],
                        all_tp=outcome["all_tp"], claim_confidence=confidence,
                        claimed_at=m.message_date, raw_text=(m.text or "")[:2000])
                    .on_conflict_do_nothing(constraint="uq_signal_claim_msg"))
                added += res.rowcount or 0
        except Exception as exc:                      # never wedge the pass again
            skipped += 1
            if len(errors) < MAX_REPORTED_ERRORS:
                errors.append({"message_id": m.id, "error": str(exc)[:200]})
            log.warning("link_claims: message %s failed, skipping: %s", m.id, exc)

    if max_id > hwm:
        await set_setting(session, _HWM_KEY, max_id)
    await session.commit()
    # Log every pass that did anything OR failed anything — a silent failure is
    # what made this cost three days.
    if added or skipped:
        log.info("link_claims: scanned %s, added %s, skipped %s (hwm %s->%s)",
                 len(msgs), added, skipped, hwm, max_id)
    return {"scanned": len(msgs), "added": added, "skipped": skipped,
            "errors": errors, "hwm": max_id}
