from fastapi import APIRouter, Depends
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.analysis.bayes import posterior
from beacon_core.execution import attribution as ATTR
from beacon_core.db.models import Leg, Signal, Source, Trade
from beacon_core.timeutil import parse_iso_utc as _parse_dt
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/performance", tags=["performance"],
                   dependencies=[Depends(require_token)])


def _leg_dates(q, frm, to):
    """Filter a query by the leg CLOSE time — the realized-in-period anchor."""
    if frm is not None:
        q = q.where(Leg.closed_at >= frm)
    if to is not None:
        q = q.where(Leg.closed_at < to)
    return q


@router.get("/summary")
async def summary(account_id: int | None = None, date_from: str | None = None,
                  date_to: str | None = None, db: AsyncSession = Depends(get_db)):
    frm, to = _parse_dt(date_from), _parse_dt(date_to)
    q = select(Leg).where(Leg.status == "closed")
    if account_id is not None:
        q = q.join(Trade, Trade.id == Leg.trade_id).where(Trade.account_id == account_id)
    q = _leg_dates(q, frm, to)
    every = (await db.execute(q)).scalars().all()
    # The AUDITABLE book (#234): every figure below is computed over the legs
    # whose money was settled against their own position. Counting the rest
    # would put 53,068.3 AED of other positions' closes into this total, and
    # 555 of the 5,061 legs are affected -- enough to move the win rate by four
    # points, not a rounding difference.
    closed = [l for l in every if ATTR.is_auditable(l.pl_attribution)]
    wins = [l for l in closed if l.outcome == "tp_hit"]
    losses = [l for l in closed if l.outcome == "sl_hit"]
    book = ATTR.auditable_pl(every)
    win_rate = (len(wins) / len(closed) * 100.0) if closed else 0.0
    gross_win = sum((float(l.realized_pl) for l in wins if l.realized_pl), 0.0)
    gross_loss = abs(sum((float(l.realized_pl) for l in losses if l.realized_pl), 0.0))
    pf = (gross_win / gross_loss) if gross_loss else None
    return {"total_pl": book["pl"], "win_rate": round(win_rate, 2),
            "closed_legs": len(closed), "wins": len(wins), "losses": len(losses),
            "profit_factor": round(pf, 2) if pf else None,
            "basis": "auditable",
            # Never the total alone. Excluding these makes the book read better
            # while the legs really traded, so the restated figure is
            # INCOMPLETE rather than corrected, and a caller has to be able to
            # say by how much.
            "excluded_legs": book["excluded_legs"],
            "excluded_pl": book["excluded_pl"],
            "as_reported": book["as_reported"],
            "attribution": _attribution(every)}


def _attribution(closed) -> dict:
    """How much of `total_pl` rests on money settled against the leg's OWN
    position (#234).

    The headline `total_pl` is now the auditable basis, so this is the receipt
    for what was left out: which basis each excluded leg was on and what it was
    carrying. 517 of them hold another position's amount to the cent -- across
    lots differing by up to 3.6x -- and 38 more simply cannot be traced."""
    out = {"unauditable_legs": 0, "unauditable_pl": 0.0, "by_basis": {}}
    for l in closed:
        basis = l.pl_attribution or ATTR.ATTR_EXACT
        pl = float(l.realized_pl) if l.realized_pl is not None else 0.0
        b = out["by_basis"].setdefault(basis, {"legs": 0, "pl": 0.0})
        b["legs"] += 1
        b["pl"] = round(b["pl"] + pl, 2)
        if not ATTR.is_auditable(l.pl_attribution):
            out["unauditable_legs"] += 1
            out["unauditable_pl"] = round(out["unauditable_pl"] + pl, 2)
    return out


@router.get("/by_source")
async def by_source(account_id: int | None = None, min_significant: int = 30,
                    date_from: str | None = None, date_to: str | None = None,
                    db: AsyncSession = Depends(get_db)):
    """Per-source: realized P&L and per-TP hit counts, plus a TRADE-level
    significance read (N vs threshold + a Beta-Binomial credible interval on the
    win rate) so a 2/2 source isn't mistaken for a real edge."""
    frm, to = _parse_dt(date_from), _parse_dt(date_to)
    q = (select(Source.id, Source.name,
                Leg.tp_index, Leg.outcome,
                func.count(Leg.id), func.coalesce(func.sum(Leg.realized_pl), 0))
         .select_from(Leg)
         .join(Trade, Trade.id == Leg.trade_id)
         .join(Signal, Signal.id == Trade.signal_id)
         .outerjoin(Source, Source.id == Signal.source_id)   # keep orphaned (source-deleted) trades
         .where(Leg.status == "closed")
         # Same auditable basis as /summary (#234), or the per-channel numbers
         # and the total would be on different books. It is not cosmetic here:
         # source 12 reads -37,660.8 across every leg and -2,945.0 across the
         # ones we can audit, and it is one of the four channels the live
         # channel-selection arm skips.
         .where(or_(Leg.pl_attribution.is_(None),
                    Leg.pl_attribution == ATTR.ATTR_EXACT))
         .group_by(Source.id, Source.name, Leg.tp_index, Leg.outcome))
    if account_id is not None:
        q = q.where(Trade.account_id == account_id)
    q = _leg_dates(q, frm, to)
    rows = (await db.execute(q)).all()
    agg: dict = {}
    for sid, sname, tp_index, outcome, cnt, pl in rows:
        s = agg.setdefault(sid, {"source_id": sid, "name": sname or "Unattributed", "pl": 0.0,
                                 "tp_hits": {}, "sl_hits": 0})
        s["pl"] += float(pl)
        if outcome == "tp_hit":
            s["tp_hits"][tp_index] = s["tp_hits"].get(tp_index, 0) + cnt
        elif outcome == "sl_hit":
            s["sl_hits"] += cnt

    # Trade-level N and wins per source (win = realized P&L > 0), for significance.
    tq = (select(Source.id, Source.name, func.count(Trade.id),
                 func.coalesce(func.sum(case((Trade.realized_pl > 0, 1), else_=0)), 0))
          .select_from(Trade)
          .join(Signal, Signal.id == Trade.signal_id)
          .outerjoin(Source, Source.id == Signal.source_id)   # include orphaned trades
          .where(Trade.status == "closed")
          .group_by(Source.id, Source.name))
    if account_id is not None:
        tq = tq.where(Trade.account_id == account_id)
    if frm is not None or to is not None:
        # a trade counts for the period if any of its legs closed in it
        cids = _leg_dates(select(Leg.trade_id).where(Leg.status == "closed"), frm, to)
        tq = tq.where(Trade.id.in_(cids))
    trows = (await db.execute(tq)).all()

    total_n = sum(int(n) for _, _, n, _ in trows)
    total_w = sum(int(w) for _, _, _, w in trows)
    base_rate = (total_w / total_n) if total_n else 0.5

    for sid, sname, n, w in trows:
        n, w = int(n), int(w)
        s = agg.setdefault(sid, {"source_id": sid, "name": sname or "Unattributed", "pl": 0.0,
                                 "tp_hits": {}, "sl_hits": 0})
        post = posterior(w, n, base_rate) if n else None
        s["n_trades"] = n
        s["wins"] = w
        s["win_rate"] = round(w / n * 100.0, 1) if n else None
        s["significant"] = n >= min_significant
        s["min_trades"] = min_significant
        s["ci"] = ({"low": round(post["ci_low"] * 100, 1),
                    "mean": round(post["mean"] * 100, 1),
                    "high": round(post["ci_high"] * 100, 1)} if post else None)

    # Sources with closed legs but no closed trades yet: fill defaults.
    for s in agg.values():
        s.setdefault("n_trades", 0)
        s.setdefault("wins", 0)
        s.setdefault("win_rate", None)
        s.setdefault("significant", False)
        s.setdefault("min_trades", min_significant)
        s.setdefault("ci", None)
    return list(agg.values())


@router.get("/equity_curve")
async def equity_curve(account_id: int | None = None, date_from: str | None = None,
                       date_to: str | None = None, db: AsyncSession = Depends(get_db)):
    """Cumulative realized P&L over time, one point per closed leg (in close
    order). This is the 'is my account growing?' curve. No stored balance
    history exists yet, so this is derived from realized results — scope it to
    one account with account_id, or omit for the whole book."""
    frm, to = _parse_dt(date_from), _parse_dt(date_to)
    q = (select(Leg.closed_at, Leg.realized_pl)
         .join(Trade, Trade.id == Leg.trade_id)
         .where(Leg.status == "closed", Leg.closed_at.isnot(None))
         # Auditable basis, matching /summary (#234). A curve that ended at a
         # different number from the headline total would be read as one of
         # them being broken.
         .where(or_(Leg.pl_attribution.is_(None),
                    Leg.pl_attribution == ATTR.ATTR_EXACT))
         .order_by(Leg.closed_at.asc()))
    if account_id is not None:
        q = q.where(Trade.account_id == account_id)
    q = _leg_dates(q, frm, to)
    rows = (await db.execute(q)).all()
    cum = 0.0
    out = []
    for closed_at, pl in rows:
        cum += float(pl or 0)
        out.append({"t": closed_at.isoformat(), "pl": round(cum, 2)})
    return out
