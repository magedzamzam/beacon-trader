"""Link channel outcome follow-ups to the signal they refer to and persist them
as SignalClaim rows. Incremental (high-water mark on message id) and idempotent
(unique on message_id). Zero impact on the trading path."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db.models import SignalClaim, TelegramMessage
from ..logging import get_logger
from ..parsing.outcomes import parse_outcome
from ..settings_store import get_setting, set_setting

log = get_logger("reconcile.claims")

_HWM_KEY = "reconcile_hwm"


async def _resolve_signal(session, msg: TelegramMessage, max_hours: int):
    """Resolve an outcome message to its signal: prefer the Telegram reply link,
    else the most recent signal in the same chat within `max_hours` before it."""
    if msg.reply_to_message_id:
        parent = (await session.execute(select(TelegramMessage).where(
            TelegramMessage.chat_id == msg.chat_id,
            TelegramMessage.message_id == msg.reply_to_message_id))).scalars().first()
        if parent and parent.signal_id:
            return parent.signal_id

    if msg.message_date is None:
        return None
    lo = msg.message_date - dt.timedelta(hours=max_hours)
    parent = (await session.execute(select(TelegramMessage).where(
        TelegramMessage.chat_id == msg.chat_id,
        TelegramMessage.signal_id.isnot(None),
        TelegramMessage.message_date <= msg.message_date,
        TelegramMessage.message_date >= lo)
        .order_by(TelegramMessage.message_date.desc()))).scalars().first()
    return parent.signal_id if parent else None


async def link_claims(session, *, max_hours: int = 12, full: bool = False,
                      cap: int = 20000) -> dict:
    """Process non-signal messages newer than the high-water mark; for each that
    parses as an outcome and resolves to a signal, upsert a SignalClaim."""
    hwm = 0 if full else int(await get_setting(session, _HWM_KEY, 0) or 0)
    msgs = (await session.execute(
        select(TelegramMessage)
        .where(TelegramMessage.is_signal.is_(False), TelegramMessage.id > hwm)
        .order_by(TelegramMessage.id.asc()).limit(cap))).scalars().all()

    added, max_id = 0, hwm
    for m in msgs:
        max_id = max(max_id, m.id)
        outcome = parse_outcome(m.text or "")
        if not outcome:
            continue
        sig_id = await _resolve_signal(session, m, max_hours)
        if sig_id is None:
            continue
        res = await session.execute(
            pg_insert(SignalClaim).values(
                signal_id=sig_id, source_id=m.source_id, message_id=m.id,
                max_tp_claimed=outcome["max_tp"], sl_claimed=outcome["sl_hit"],
                all_tp=outcome["all_tp"], claimed_at=m.message_date,
                raw_text=(m.text or "")[:2000])
            .on_conflict_do_nothing(constraint="uq_signal_claim_msg"))
        added += res.rowcount or 0

    if max_id > hwm:
        await set_setting(session, _HWM_KEY, max_id)
    await session.commit()
    if added:
        log.info("link_claims: scanned %s, added %s claims (hwm %s->%s)",
                 len(msgs), added, hwm, max_id)
    return {"scanned": len(msgs), "added": added, "hwm": max_id}
