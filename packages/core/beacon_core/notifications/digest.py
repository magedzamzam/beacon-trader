"""The daily operator digest (#198).

`daily_summary` sat in the routing matrix with an emoji and an empty field
contract for months: routable, greyed out in the editor, and fired by nothing.
This is the emitter — an end-of-day roll-up of what happened, so the operator
does not have to tail a box or open the portal to know how the day went.

TWO DECISIONS WORTH KNOWING:

**It reports the last COMPLETE UTC day, never a partial one.** `at_utc` says
when after midnight to send yesterday's digest (default 00:15). A digest of a
day still in progress is a number that changes after you have read it.

**P&L is broker truth, with no fallback to the ledger.** `trades.realized_pl` is
the ledger's own accumulation and is currently 35–106% adrift from what the
account settled (#202), so a digest built on it would show the operator a book
wrong by thousands per week. `analysis/broker_truth` dedupes
`position_activities` on the identity the table itself declares. A trade the
broker has not settled is simply not in that day's digest — reported late is
recoverable, reported wrong is not.

Read-only over the ledger. This is reporting; it never touches a position.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import select

from ..analysis import broker_truth as BT
from ..db.models import Account, PositionActivity, Trade

DEFAULT_AT_UTC = "00:15"


def _parse_hhmm(s: str) -> dt.time:
    try:
        h, m = str(s).split(":")[:2]
        return dt.time(int(h) % 24, int(m) % 60)
    except Exception:                              # never let bad config stop the digest
        h, m = DEFAULT_AT_UTC.split(":")
        return dt.time(int(h), int(m))


def digest_due(last_reported: Optional[str], now: dt.datetime,
               at_utc: str = DEFAULT_AT_UTC) -> Optional[str]:
    """The UTC date this tick should report, or None.

    `last_reported` is the DATE the last digest covered, not when it was sent —
    which is what makes "exactly one per day" survive a restart, a clock change,
    and a monitor that was down when the send time passed. A monitor that comes
    back at 15:00 sends yesterday's digest once and does not then walk backwards
    filling in the week: a late digest is useful, a burst of stale ones is not.
    """
    target = str((now.date() - dt.timedelta(days=1)))
    if now.timetz().replace(tzinfo=None) < _parse_hhmm(at_utc):
        return None                                # not yet time to send
    if last_reported == target:
        return None                                # already sent for that day
    return target


def _fmt_money(v) -> str:
    sign = "+" if float(v) >= 0 else ""
    return f"{sign}{float(v):,.2f}"


async def build_digests(session, day: str) -> list[dict]:
    """One ctx per account that did anything on `day` (a UTC date string).

    Per account rather than one combined figure: the three demo accounts are
    A/B/C arms of a running experiment, and their sum is a number that describes
    no strategy. `{account}` is what tells the arms apart in the message."""
    start = dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(days=1)

    # A trade counts for the day its money LANDED, which is what the activity
    # timestamp records. `trades` has no `closed_at` to key on, and the ledger
    # figure is the one that is adrift (#202) — so the activity ledger is both
    # the clock and the amount here, with no silent fallback to `realized_pl`.
    # A trade the broker has not settled simply is not in this day's digest yet.
    acts = (await session.execute(
        select(PositionActivity).where(
            PositionActivity.activity_at >= start,
            PositionActivity.activity_at < end,
            PositionActivity.realized_pl.isnot(None)))).scalars().all()
    rows = [{"account_id": a.account_id, "deal_id": a.deal_id,
             "activity_at": a.activity_at, "type": a.type,
             "trade_id": a.trade_id, "realized_pl": a.realized_pl}
            for a in acts]
    account_of = {r["trade_id"]: r["account_id"] for r in rows
                  if r["trade_id"] is not None}

    open_counts: dict = {}
    for t in (await session.execute(
            select(Trade).where(Trade.status.in_(("open", "partial"))))).scalars().all():
        open_counts[t.account_id] = open_counts.get(t.account_id, 0) + 1

    per_account: dict = {}
    for tid, pl in BT.realized_by_trade(rows).items():
        acct = account_of.get(tid)
        if acct is None:
            continue
        per_account.setdefault(acct, []).append(float(pl))

    # EVERY enabled account, including the ones that did nothing. A silent day
    # is the day worth reporting: on 2026-08-06 Arm B settled zero trades while
    # the control settled 28, and a digest that simply omitted the account would
    # have been indistinguishable from a normal quiet Sunday.
    accounts = (await session.execute(
        select(Account).where(Account.enabled.is_(True)))).scalars().all()
    by_id = {a.id: a for a in accounts}
    ids = sorted(set(by_id) | set(per_account) | set(open_counts))

    out = []
    for acct in ids:
        pls = per_account.get(acct, [])
        account = by_id.get(acct) or await session.get(Account, acct)
        out.append({
            "date": day,
            "account": account.name if account else f"account {acct}",
            "account_id": acct,
            "pl": _fmt_money(sum(pls)) if pls else "0.00",
            "wins": str(sum(1 for p in pls if p > 0)),
            "losses": str(sum(1 for p in pls if p < 0)),
            "open_positions": str(open_counts.get(acct, 0)),
            "drawdown": _fmt_money(BT.max_drawdown(pls)) if pls else "0.00",
            "detail": (f"{len(pls)} trade(s) settled on {day} · "
                       "P&L from deduped broker activity"
                       if pls else f"nothing settled on {day}"),
        })
    return out
