"""Broker truth: what the account actually settled, deduped exactly once.

`position_activities` is the broker-authoritative record. Reading it is easy to
get wrong in a way that looks right, so the dedup lives here once rather than
being re-derived by every caller:

  * The identity of an activity is `(account_id, deal_id, activity_at, type)` —
    the table's own unique constraint. Collapsing on `deal_id` alone (the
    tempting `MAX(realized_pl) GROUP BY deal_id`) silently drops every partial
    close but the largest, which on a laddered book is most of the money.
  * Rows can arrive twice from two feeds differing only by `source` (the USER/TP
    pair), so the dedup is a MAX over the identity, not a SUM over it.
  * Only then is it summed per trade.

Pure by design: the query belongs to the caller, the arithmetic belongs here, so
it can be tested without a database (#198, and the instrument #202 needs).
"""
from __future__ import annotations

from typing import Iterable, Optional

# The activity identity, in the order the unique constraint declares it.
IDENTITY = ("account_id", "deal_id", "activity_at", "type")


def _key(row: dict) -> tuple:
    return tuple(row.get(k) for k in IDENTITY)


def dedupe_activities(rows: Iterable[dict]) -> list[dict]:
    """One row per activity identity, keeping the largest `realized_pl`.

    MAX rather than "first seen": the duplicate pair differs only by `source`
    and carries the same money, so either is correct, and MAX is the choice that
    does not depend on the order the rows came back in."""
    best: dict = {}
    for r in rows or ():
        if r.get("realized_pl") is None:
            continue
        k = _key(r)
        prev = best.get(k)
        if prev is None or float(r["realized_pl"]) > float(prev["realized_pl"]):
            best[k] = r
    return list(best.values())


def realized_by_trade(rows: Iterable[dict]) -> dict:
    """`{trade_id: settled P&L}` — dedupe once, then sum per trade."""
    out: dict = {}
    for r in dedupe_activities(rows):
        tid = r.get("trade_id")
        if tid is None:
            continue
        out[tid] = out.get(tid, 0.0) + float(r["realized_pl"])
    return out


def settled_pl(rows: Iterable[dict], ledger: Optional[dict] = None) -> dict:
    """Per-trade P&L preferring broker truth, falling back to the ledger.

    Returns `{"by_trade": {...}, "n_broker": int, "n_ledger": int}` so a caller
    can SAY which basis it used instead of presenting a mixed number as if it
    had one. A trade with no activity row is not zero — it is a trade the broker
    record cannot score yet, and the ledger is the only figure available for it.
    """
    broker = realized_by_trade(rows)
    out = dict(broker)
    n_ledger = 0
    for tid, pl in (ledger or {}).items():
        if tid in out or pl is None:
            continue
        out[tid] = float(pl)
        n_ledger += 1
    return {"by_trade": out, "n_broker": len(broker), "n_ledger": n_ledger}


def signed_close_pl(pl, outcome: Optional[str]):
    """Restore the SIGN the broker did not send on a close (#202).

    Capital.com returns the transaction amount UNSIGNED when the position's stop
    was modified before it closed — a ratcheted stop-out comes back as `+89.71`.
    `_close_leg` has always repaired this for the leg (`a stop-out is always a
    loss`), but `_audit_activities` wrote the raw value straight into
    `position_activities`, so the two ledgers disagreed by exactly TWICE the
    ratcheted losses: 176 of 178 ratcheted stop-outs were stored positive
    against 2 of 1093 un-ratcheted ones.

    That is where the "phantom tax" came from, and it points the opposite way to
    how it read: `trades.realized_pl` is the SOUND figure (it sums legs, which
    are signed correctly) and the activity ledger was the inflated one. Anything
    that treats `position_activities` as truth without this repair reports a
    losing week as a winning one.

    Only `sl_hit` is corrected, exactly mirroring the leg path. A breakeven can
    legitimately land either side of zero and those rows agree already (657 of
    657 exact), so touching them would invent an error."""
    if pl is None:
        return None
    if outcome == "sl_hit" and float(pl) > 0:
        return -pl
    return pl


def phantom_gap(broker_by_trade: dict, ledger_by_trade: dict,
                *, tolerance: float = 1.0) -> dict:
    """Broker ledger vs trade ledger, per the set of trades given.

    Reported as a metric rather than reconciled away: the two are built from
    different records and a divergence means one of them stopped tracking the
    account, which is a thing to be told about, not to be silently averaged."""
    trades = set(broker_by_trade) | set(ledger_by_trade)
    broker = sum(float(broker_by_trade.get(t) or 0.0) for t in trades)
    ledger = sum(float(ledger_by_trade.get(t) or 0.0) for t in trades)
    gap = round(broker - ledger, 2)
    worst = None
    for t in trades:
        d = float(broker_by_trade.get(t) or 0.0) - float(ledger_by_trade.get(t) or 0.0)
        if worst is None or abs(d) > abs(worst[1]):
            worst = (t, round(d, 2))
    return {"n_trades": len(trades), "broker": round(broker, 2),
            "ledger": round(ledger, 2), "gap": gap,
            "worst_trade": worst[0] if worst else None,
            "worst_gap": worst[1] if worst else 0.0,
            "material": bool(abs(gap) > tolerance)}


def max_drawdown(series: Iterable[float]) -> float:
    """Deepest peak-to-trough fall of a running total, as a NEGATIVE number.

    Fed the day's realized P&L in close order, this is the worst the day got
    from its own high-water mark — which is the number an operator means by "how
    bad did it get today", and is not recoverable from the day's net."""
    peak = running = 0.0
    worst = 0.0
    for v in series or ():
        running += float(v)
        peak = max(peak, running)
        worst = min(worst, running - peak)
    return round(worst, 2)
