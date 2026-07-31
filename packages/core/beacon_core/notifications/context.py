"""Assemble a notification ctx from a real trade/signal.

Used by the test-fire endpoint (#139) to render a template against real data,
and a seed for the Layer-2 emitter enrichment. Kept out of the pure `config`
layer because it touches the DB models; best-effort — any missing piece just
leaves that token empty (the renderer treats empty as empty, never an error).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select

from ..db.models import Account, AiAssessment, Leg, Signal, Source, Trade
from ..timeutil import utcnow
from .templates import EVENT_FIELDS

# Leg states that never reached the broker — they carry a planned lot, not a
# traded one, so they must not inflate the reported size (#179).
_UNTRADED_LEG_STATUS = {"staged", "rejected", "cancelled", "expired"}


def _fmt_time(v) -> Optional[str]:
    try:
        return v.strftime("%Y-%m-%d %H:%M") if v is not None else None
    except Exception:                            # pragma: no cover - defensive
        return None


def _entry(signal) -> Optional[str]:
    """Signal entry as the executor formats it: a single price or a `from–to`
    band."""
    if signal is None or signal.entry_from is None:
        return None
    if signal.entry_to is not None and signal.entry_to != signal.entry_from:
        return f"{signal.entry_from}–{signal.entry_to}"
    return str(signal.entry_from)


async def _trade_size(session, trade) -> Optional[str]:
    """Σ lot over the trade's legs that actually reached the broker. This is the
    historical reconstruction of `{size}` — what the live emitter sends at event
    time — so a test-fire renders the same number the real alert carried."""
    legs = (await session.execute(
        select(Leg).where(Leg.trade_id == trade.id))).scalars().all()
    lots = [l.lot for l in legs
            if l.lot is not None and l.status not in _UNTRADED_LEG_STATUS]
    return str(sum(lots)) if lots else None


async def build_ctx(session, event_id: str = None, *,
                    trade: Trade = None, signal: Signal = None) -> dict:
    """Notification ctx resolved from a trade and/or signal (joining source /
    account / latest AI assessment), using explicit `session.get` (no lazy
    relationship access) so it's async-safe.

    When `event_id` is given, the result is FILTERED to that event's field
    contract (`templates.EVENT_FIELDS`) — so a test-fire renders exactly the
    fields the live emitter would carry, never a richer superset. Runtime-only
    fields the emitter computes at event time (e.g. `price`, `detail`) aren't
    reconstructable from a historical trade and are simply absent (→ empty token,
    never an error). Without `event_id`, the full reconstructable set is returned.
    """
    if signal is None and trade is not None and trade.signal_id is not None:
        signal = await session.get(Signal, trade.signal_id)
    source = (await session.get(Source, signal.source_id)
              if signal is not None and signal.source_id is not None else None)
    account = (await session.get(Account, trade.account_id)
               if trade is not None else None)

    # Only join the AI assessment when the event's contract references it — skip
    # the extra query for events (like trade_closed) that don't surface ai.*.
    want_ai = event_id is None or any(
        t.split(".")[0] == "ai" for t in EVENT_FIELDS.get(event_id, []))
    # Same idea for the leg query behind `{size}` — only pay for it when the
    # event's contract actually surfaces it.
    want_size = event_id is None or "size" in EVENT_FIELDS.get(event_id, [])
    size = await _trade_size(session, trade) if (trade is not None and want_size) else None

    ai = None
    if signal is not None and want_ai:
        ai = (await session.execute(
            select(AiAssessment).where(AiAssessment.signal_id == signal.id)
            .order_by(AiAssessment.id.desc()).limit(1))).scalars().first()

    ctx = {
        "symbol": (trade.symbol if trade else None) or (signal.symbol if signal else None),
        "direction": (trade.direction if trade else None) or (signal.direction if signal else None),
        "channel": source.name if source else None,
        "account": account.name if account else None,
        "size": size,
        "entry": _entry(signal),
        "tp": ", ".join(signal.tps) if signal and signal.tps else None,
        "sl": str(signal.sl) if signal and signal.sl is not None else None,
        "pl": str(trade.realized_pl) if trade and trade.realized_pl is not None else None,
        "open_time": _fmt_time(trade.created_at) if trade else None,
        "close_time": _fmt_time(utcnow()) if trade and trade.status == "closed" else None,
    }
    if ai is not None:
        ctx["ai"] = {"verdict": ai.verdict,
                     "confidence": str(ai.confidence) if ai.confidence is not None else None}

    if event_id is not None:
        # keep only the roots in this event's contract, so test == live emitter
        roots = {t.split(".")[0] for t in EVENT_FIELDS.get(event_id, [])}
        ctx = {k: v for k, v in ctx.items() if k in roots}
    return ctx
