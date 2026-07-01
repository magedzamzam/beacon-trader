"""Shared ingest: structured signal -> validate -> persist -> publish."""
from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal

from sqlalchemy import select

from beacon_core.bus import Bus
from beacon_core.config import CH_SIGNAL_VALID
from beacon_core.db.models import Signal
from beacon_core.parsing.models import ParsedSignal
from beacon_core.execution.planner import validate_signal

bus = Bus()


def _hash(source_id, symbol, direction, entry_from, sl) -> str:
    key = f"{source_id}:{symbol}:{direction}:{entry_from}:{sl}"
    return hashlib.sha256(key.encode()).hexdigest()


async def ingest_structured(session, *, source_id, symbol, direction, entry_from,
                            entry_to, sl, tps, order_type, raw_text=""):
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
    await session.commit()
    if ok:
        await bus.publish(CH_SIGNAL_VALID, {"signal_id": sig.id})
    return sig.id, ok, reason
