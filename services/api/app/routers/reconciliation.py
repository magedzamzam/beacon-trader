"""Live Reconciler API: compare what a channel CLAIMED happened (signal_claims)
against what the bot ACTUALLY did (trades/legs), per signal, with a category for
every divergence. Claims are (re)linked lazily so the view is always current."""
import datetime as dt
from collections import Counter, defaultdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.analysis.claims import link_claims
from beacon_core.analysis.reconcile import (reconcile_signal, override_to_claim,
                                            valid_override, is_protected)
from beacon_core.db.models import Event, Leg, SignalClaim, Signal, Source, Trade
from beacon_core.timeutil import parse_iso_utc as _parse_dt
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"],
                   dependencies=[Depends(require_token)])

# Event kinds that mean the bot DELIBERATELY did not place a trade (#136 pt2). A
# zero-leg signal carrying one of these is protection, not a "said executed, placed
# nothing" bug — so it's excluded from the match-rate denominator.
_BLOCK_KINDS = ("risk_blocked", "ai_blocked", "breaker_state",
                "blocked_untrusted", "entry_filtered")


async def _blocked_by_signal(db, sig_ids) -> dict:
    """signal_id -> the block-event kind that protected it (if any). Block events
    are rare relative to signals; signal_id lives in the Event.payload JSON."""
    if not sig_ids:
        return {}
    evs = (await db.execute(select(Event).where(Event.kind.in_(_BLOCK_KINDS)))).scalars().all()
    wanted = set(sig_ids)
    out = {}
    for e in evs:
        sid = (e.payload or {}).get("signal_id")
        if sid in wanted and sid not in out:
            out[sid] = e.kind
    return out


async def _build_rows(db, frm, to, source_id, include_history):
    claims = (await db.execute(select(SignalClaim))).scalars().all()
    by_sig = defaultdict(list)
    for c in claims:
        by_sig[c.signal_id].append(c)
    if not by_sig:
        return []

    sig_ids = list(by_sig.keys())
    sq = (select(Signal, Source.name)
          .outerjoin(Source, Source.id == Signal.source_id)
          .where(Signal.id.in_(sig_ids)))
    if source_id is not None:
        sq = sq.where(Signal.source_id == source_id)
    if frm is not None:
        sq = sq.where(Signal.created_at >= frm)
    if to is not None:
        sq = sq.where(Signal.created_at < to)
    sig_rows = (await db.execute(sq)).all()
    if not sig_rows:
        return []

    kept_ids = [s.id for (s, _) in sig_rows]
    blocked_by_sig = await _blocked_by_signal(db, kept_ids)
    trades = (await db.execute(select(Trade).where(Trade.signal_id.in_(kept_ids)))).scalars().all()
    trades_by_sig = defaultdict(list)
    for t in trades:
        trades_by_sig[t.signal_id].append(t)
    trade_ids = [t.id for t in trades]
    legs = ((await db.execute(select(Leg).where(Leg.trade_id.in_(trade_ids)))).scalars().all()
            if trade_ids else [])
    legs_by_trade = defaultdict(list)
    for l in legs:
        legs_by_trade[l.trade_id].append(l)

    rows = []
    for sig, sname in sig_rows:
        is_history = sig.status == "history"
        if is_history and not include_history:
            continue
        sig_legs = [l for t in trades_by_sig.get(sig.id, []) for l in legs_by_trade.get(t.id, [])]
        leg_dicts = [{
            "tp_index": l.tp_index, "status": l.status, "outcome": l.outcome,
            "entry": float(l.entry), "tp": float(l.tp), "sl": float(l.sl),
            "fill_price": float(l.fill_price) if l.fill_price is not None else None,
            "close_price": float(l.close_price) if l.close_price is not None else None,
            "realized_pl": float(l.realized_pl) if l.realized_pl is not None else None,
        } for l in sorted(sig_legs, key=lambda x: (x.tp_index or 0, x.id))]

        sig_claims = sorted(by_sig[sig.id], key=lambda c: (c.claimed_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc)))
        n_tps = len(sig.tps or [])
        # An operator override (#136 pt3) takes precedence over the parsed claim.
        claim_dicts = []
        for c in sig_claims:
            ov = override_to_claim(c.override_outcome, n_tps)
            claim_dicts.append(ov if ov is not None else
                               {"max_tp_claimed": c.max_tp_claimed, "sl_claimed": c.sl_claimed,
                                "all_tp": c.all_tp})
        blocked_kind = blocked_by_sig.get(sig.id)
        rec = reconcile_signal(signal_status=sig.status, n_signal_tps=n_tps,
                               is_history=is_history, claims=claim_dicts, legs=leg_dicts,
                               blocked=blocked_kind is not None)
        protected = is_protected(rec["category"])
        protected_reason = ((sig.reject_reason or blocked_kind or sig.status)
                            if protected else None)
        rows.append({
            "signal_id": sig.id, "source_id": sig.source_id, "source_name": sname,
            "symbol": sig.symbol, "direction": sig.direction, "status": sig.status,
            "created_at": sig.created_at.isoformat() if sig.created_at else None,
            "signal_text": sig.raw_text,
            "trade_ids": [t.id for t in sorted(trades_by_sig.get(sig.id, []), key=lambda x: x.id)],
            "claimed_max_tp": rec["claimed_max_tp"], "claimed_sl": rec["claimed_sl"],
            "bot_max_tp": rec["bot_max_tp"], "bot_any_fill": rec["bot_any_fill"],
            "category": rec["category"], "detail": rec["detail"], "is_history": is_history,
            "protected": protected, "protected_reason": protected_reason,
            "claims": [{"id": c.id, "max_tp": c.max_tp_claimed, "sl": c.sl_claimed, "all_tp": c.all_tp,
                        "text": c.raw_text, "at": c.claimed_at.isoformat() if c.claimed_at else None,
                        "override_outcome": c.override_outcome, "override_note": c.override_note}
                       for c in sig_claims],
            "legs": leg_dicts,
        })
    return rows


@router.post("/refresh")
async def refresh(full: bool = False, db: AsyncSession = Depends(get_db)):
    """Force a (re)link pass over telegram messages -> signal_claims."""
    return await link_claims(db, full=full)


class OverrideIn(BaseModel):
    override_outcome: str | None = None   # sl_hit | tp1 | tp2 | … | all_tp | breakeven | none/null
    override_note: str | None = None


@router.post("/claims/{claim_id}/override")
async def set_claim_override(claim_id: int, body: OverrideIn,
                             db: AsyncSession = Depends(get_db)):
    """Operator correction (#136 pt3): force-tag a follow-up message's outcome when
    the parser misread it. `override_outcome=null`/'none' clears the override so the
    parsed value stands again. The reconciler recomputes the signal on next load."""
    val = (body.override_outcome or "").strip().lower() or None
    if val == "none":
        val = None
    if not valid_override(val):
        raise HTTPException(422, f"invalid override_outcome: {body.override_outcome!r}")
    claim = (await db.execute(select(SignalClaim).where(
        SignalClaim.id == claim_id))).scalars().first()
    if not claim:
        raise HTTPException(404, "claim not found")
    claim.override_outcome = val
    claim.override_note = (body.override_note or None)
    claim.override_at = dt.datetime.now(dt.timezone.utc) if val else None
    await db.commit()
    return {"ok": True, "claim_id": claim_id, "override_outcome": val,
            "override_note": claim.override_note}


@router.get("/summary")
async def summary(date_from: str = None, date_to: str = None, source_id: int = None,
                  include_history: bool = False, db: AsyncSession = Depends(get_db)):
    try:
        await link_claims(db)                       # keep claims fresh (incremental)
    except Exception:
        pass
    rows = await _build_rows(db, _parse_dt(date_from), _parse_dt(date_to),
                             source_id, include_history)
    cats = Counter(r["category"] for r in rows)
    # Protection-driven non-execution is excluded from the match-rate denominator
    # (#136 pt2) — the bot deliberately didn't trade, so it's not a shortfall.
    protected_reasons = Counter(r["protected_reason"] for r in rows if r["protected"])
    by_source = {}
    for r in rows:
        s = by_source.setdefault(r["source_id"], {"source_id": r["source_id"],
                                                  "name": r["source_name"], "match": 0,
                                                  "total": 0, "protected": 0})
        if r["protected"]:
            s["protected"] += 1
            continue                                # excluded from this channel's rate
        s["total"] += 1                             # evaluable signals only
        if r["category"] == "match":
            s["match"] += 1
    for s in by_source.values():
        s["rate"] = round(s["match"] / s["total"] * 100, 1) if s["total"] else None
    total = len(rows)
    protected = sum(1 for r in rows if r["protected"])
    evaluable = total - protected                   # signals the bot actually engaged
    matched = cats.get("match", 0)
    return {
        "total": total, "matched": matched,
        "protected": protected, "evaluable": evaluable,
        "protected_reasons": dict(protected_reasons),
        "match_rate": round(matched / evaluable * 100, 1) if evaluable else None,
        "categories": dict(cats),
        "by_source": sorted(by_source.values(), key=lambda x: -(x["total"] + x["protected"])),
    }


@router.get("")
async def list_rows(date_from: str = None, date_to: str = None, source_id: int = None,
                    category: str = None, include_history: bool = False,
                    limit: int = 300, db: AsyncSession = Depends(get_db)):
    try:
        await link_claims(db)
    except Exception:
        pass
    rows = await _build_rows(db, _parse_dt(date_from), _parse_dt(date_to),
                             source_id, include_history)
    if category:
        rows = [r for r in rows if r["category"] == category]
    rows.sort(key=lambda r: r["created_at"] or "", reverse=True)
    return rows[:limit]
