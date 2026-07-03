"""Shared ingest: structured signal -> validate -> persist -> publish."""
from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal

from sqlalchemy import select

from beacon_core.ai import service as ai_service
from beacon_core.bus import Bus
from beacon_core.config import CH_SIGNAL_VALID
from beacon_core.db.models import Signal, Source
from beacon_core.logging import get_logger
from beacon_core.parsing.models import ParsedSignal
from beacon_core.execution.planner import validate_signal

bus = Bus()
log = get_logger("ingest")


def _hash(source_id, symbol, direction, entry_from, sl) -> str:
    key = f"{source_id}:{symbol}:{direction}:{entry_from}:{sl}"
    return hashlib.sha256(key.encode()).hexdigest()


async def ingest_structured(session, *, source_id, symbol, direction, entry_from,
                            entry_to, sl, tps, order_type, raw_text="",
                            from_freetext=False):
    parsed = ParsedSignal(
        symbol=symbol, direction=direction.upper(),
        entry_from=Decimal(str(entry_from)), entry_to=Decimal(str(entry_to)),
        sl=Decimal(str(sl)), tps=[Decimal(str(t)) for t in tps],
        order_type_hint=order_type, raw_text=raw_text,
    )
    ok, reason = validate_signal(parsed)
    dedupe = _hash(source_id, symbol, direction, entry_from, sl)
    recent = (await session.execute(select(Signal).where(
        Signal.dedupe_hash == dedupe,
        Signal.created_at >= dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
    ))).scalars().first()
    if recent:
        return recent.id, False, "duplicate"

    sig = Signal(
        source_id=source_id, symbol=symbol, direction=direction.upper(),
        entry_from=parsed.entry_from, entry_to=parsed.entry_to, sl=parsed.sl,
        tps=[str(t) for t in parsed.tps], order_type=order_type,
        status="validated" if ok else "rejected",
        reject_reason=None if ok else reason, raw_text=raw_text, dedupe_hash=dedupe,
    )
    session.add(sig)
    await session.flush()

    # Free-text signals (parsed from a chat/webhook message) are validated and
    # corrected by the AI before they can trade — the parser can misread levels.
    # Structured/manual signals are treated as already confirmed and skip this.
    if ok and from_freetext:
        try:
            source = await session.get(Source, source_id) if source_id else None
            status = await ai_service.apply_signal_validation(session, sig, source)
            if status == "rejected":
                ok = False
                reason = sig.reject_reason or "AI rejected the signal"
        except Exception as exc:                 # never break ingestion
            log.warning("AI signal validation failed: %s", exc)

    await session.commit()
    if ok:
        await bus.publish(CH_SIGNAL_VALID, {"signal_id": sig.id})
    return sig.id, ok, reason
