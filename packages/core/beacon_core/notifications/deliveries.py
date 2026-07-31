"""Delivery telemetry (#181): persist the per-channel outcome dispatch already
computes, so "did my last SL-hit alert actually reach Telegram?" has an answer.

Bounded ring, not a ledger — rows past `MAX_ROWS` are trimmed, writing one is
best-effort, and nothing reads it back to make a decision. The trading path is
untouched: `notify()` is already fire-and-forget on its own session.
"""
from __future__ import annotations

from sqlalchemy import delete, func, select

from ..db.models import NotificationDelivery
from ..logging import get_logger
from . import config as C

log = get_logger("notifications")

MAX_ROWS = 500          # how much tail we keep
SUBJECT_MAX = 200       # matches the column
_TRIM_EVERY = 25        # amortise the prune over writes

_EVENT_LABEL = {e["id"]: e["label"] for g in C.EVENT_GROUPS for e in g["events"]}
_writes_since_trim = 0


async def record(session, event_id: str, subject: str | None, results: dict) -> None:
    """Append one dispatch outcome. Swallows everything — a telemetry write must
    never be the reason a notification (or a trade) misbehaves."""
    global _writes_since_trim
    results = dict(results or {})
    try:
        session.add(NotificationDelivery(
            event_id=str(event_id)[:32],
            subject=(subject or "")[:SUBJECT_MAX] or None,
            results=results,
            ok=any(v == "ok" for v in results.values())))
        await session.commit()
        _writes_since_trim += 1
        if _writes_since_trim >= _TRIM_EVERY:
            _writes_since_trim = 0
            await _trim(session)
    except Exception as exc:                     # pragma: no cover - defensive
        log.debug("delivery log write failed (%s): %s", event_id, exc)
        try:
            await session.rollback()
        except Exception:
            pass


async def _trim(session) -> None:
    """Keep the newest MAX_ROWS. Trims by id (monotonic) rather than by age, so
    the bound holds whatever the notification rate turns out to be."""
    newest = (await session.execute(select(func.max(NotificationDelivery.id)))).scalar()
    if newest is None or newest <= MAX_ROWS:
        return
    await session.execute(delete(NotificationDelivery).where(
        NotificationDelivery.id <= newest - MAX_ROWS))
    await session.commit()


def to_dict(row) -> dict:
    return {
        "id": row.id,
        "ts": row.created_at.isoformat() if row.created_at is not None else None,
        "event": row.event_id,
        "label": _EVENT_LABEL.get(row.event_id, row.event_id),
        "subject": row.subject,
        "results": row.results or {},
        "ok": bool(row.ok),
    }


async def recent(session, limit: int = 50) -> list[dict]:
    """The newest `limit` dispatches, newest first."""
    limit = max(1, min(int(limit or 50), MAX_ROWS))
    rows = (await session.execute(
        select(NotificationDelivery)
        .order_by(NotificationDelivery.id.desc())
        .limit(limit))).scalars().all()
    return [to_dict(r) for r in rows]
