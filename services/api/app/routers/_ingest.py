"""Structured/webhook ingest — a thin wrapper over the shared pipeline (#35).
Kept as a stable entry point for the signals router; all parse/validate/dedupe/
persist/publish logic lives in beacon_core.ingest.pipeline."""
from __future__ import annotations

from decimal import Decimal

from beacon_core.ingest import InboundMessage, ingest_message
from beacon_core.logging import get_logger
from beacon_core.parsing.models import ParsedSignal

log = get_logger("ingest")


async def ingest_structured(session, *, source_id, symbol, direction, entry_from,
                            entry_to, sl, tps, order_type, raw_text="",
                            from_freetext=False):
    """Ingest an already-structured signal. Returns (signal_id, accepted, reason)
    for backward compatibility with the signals router."""
    parsed = ParsedSignal(
        symbol=symbol, direction=direction.upper(),
        entry_from=Decimal(str(entry_from)), entry_to=Decimal(str(entry_to)),
        sl=Decimal(str(sl)), tps=[Decimal(str(t)) for t in tps],
        order_type_hint=order_type, raw_text=raw_text,
    )
    res = await ingest_message(session, InboundMessage(
        source_id=source_id, kind="api", text=raw_text, parsed=parsed,
        from_freetext=from_freetext, is_live=True))
    return res.signal_id, res.accepted, res.reason
