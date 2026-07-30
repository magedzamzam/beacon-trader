"""Live Reconciler API: compare what a channel CLAIMED happened (signal_claims)
against what the bot ACTUALLY did (trades/legs), per signal, with a category for
every divergence. Claims are (re)linked lazily so the view is always current."""
import datetime as dt
from collections import Counter, defaultdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.analysis.claims import link_claims
from beacon_core.logging import get_logger
from beacon_core.analysis.reconcile import (reconcile_signal, override_to_claim,
                                            valid_override, is_protected,
                                            is_uncomparable)
from beacon_core.db.models import (Event, Leg, SignalClaim, Signal, Source,
                                   TelegramMessage, Trade)
from beacon_core.timeutil import parse_iso_utc as _parse_dt
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"],
                   dependencies=[Depends(require_token)])

log = get_logger("api.reconciliation")

# Event kinds that mean the bot DELIBERATELY did not place a trade (#136 pt2). A
# zero-leg signal carrying one of these is protection, not a "said executed, placed
# nothing" bug — so it's excluded from the match-rate denominator.
_BLOCK_KINDS = ("risk_blocked", "ai_blocked", "breaker_state",
                "blocked_untrusted", "entry_filtered")


async def _link_claims_logged(db) -> None:
    """Keep claims fresh, and NEVER fail silently again (#173).

    These call sites used to be `except Exception: pass`, which is how a wedged
    linker cost three days of claims with no log line anywhere. Linking must not
    break the page — the Reconciler is still readable with stale claims — but the
    failure has to be audible."""
    try:
        await link_claims(db)
    except Exception as exc:                        # pragma: no cover - defensive
        log.exception("link_claims failed; Reconciler is showing STALE claims: %s", exc)
        try:
            await db.rollback()                     # leave the session usable
        except Exception:
            pass


async def _linker_health(db) -> dict:
    """How far behind the claim linker is (#173). This failure mode is otherwise
    completely invisible — the bot keeps trading and the page keeps rendering,
    just with no new claims — so the number belongs on the page."""
    from beacon_core.analysis.claims import _HWM_KEY
    from beacon_core.settings_store import get_setting
    try:
        hwm = int(await get_setting(db, _HWM_KEY, 0) or 0)
        max_id = (await db.execute(select(func.max(TelegramMessage.id)))).scalar() or 0
        unscanned = (await db.execute(
            select(func.count()).select_from(TelegramMessage)
            .where(TelegramMessage.is_signal.is_(False),
                   TelegramMessage.id > hwm))).scalar() or 0
        last_claim = (await db.execute(select(func.max(SignalClaim.claimed_at)))).scalar()
        return {"hwm": hwm, "max_message_id": max_id, "unscanned": int(unscanned),
                "last_claim_at": last_claim.isoformat() if last_claim else None}
    except Exception as exc:                        # never break the summary
        log.warning("linker health check failed: %s", exc)
        return {}


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

    # #172: the candidate set is every signal with a claim OR at least one trade.
    # Starting from claims alone made 38% of traded signals structurally invisible
    # — and they were the losing 38% (claimed 65% win / +20k, unclaimed 33% / -207k),
    # so the match rate was measuring what channels chose to announce. A signal with
    # neither a claim nor a trade has nothing to reconcile and stays out.
    traded_ids = set((await db.execute(
        select(Trade.signal_id).distinct())).scalars().all())
    sig_ids = list(set(by_sig.keys()) | traded_ids)
    if not sig_ids:
        return []
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
        # Trade-level realized P&L (CLAUDE.md §2.5 — never leg-level), so the
        # summary can show what the unclaimed cohort actually did (#172).
        sig_trades = trades_by_sig.get(sig.id, [])
        pls = [float(t.realized_pl) for t in sig_trades if t.realized_pl is not None]
        net_pl = round(sum(pls), 2) if pls else None
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
            "uncomparable": is_uncomparable(rec["category"]), "net_pl": net_pl,
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
    await _link_claims_logged(db)                   # keep claims fresh (incremental)
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
                                                  "total": 0, "protected": 0,
                                                  "uncomparable": 0})
        if r["protected"]:
            s["protected"] += 1
            continue                                # excluded from this channel's rate
        if r["uncomparable"]:
            s["uncomparable"] += 1                  # traded, but the channel went quiet
            continue
        s["total"] += 1                             # comparable signals only
        if r["category"] == "match":
            s["match"] += 1
    for s in by_source.values():
        s["rate"] = round(s["match"] / s["total"] * 100, 1) if s["total"] else None
        seen = s["total"] + s["uncomparable"]
        # What share of this channel's traded signals it actually reported an
        # outcome for. A high rate on low coverage is the channel's PR, not its
        # performance (#172).
        s["claim_coverage"] = round(s["total"] / seen * 100, 1) if seen else None
    total = len(rows)
    protected = sum(1 for r in rows if r["protected"])
    uncomparable = sum(1 for r in rows if r["uncomparable"])
    evaluable = total - protected                   # signals the bot actually engaged
    comparable = evaluable - uncomparable           # ...and the channel scored
    matched = cats.get("match", 0)

    def _cohort(pred):
        """Realized outcome of a cohort, so the selection bias is visible on the
        page instead of having to be discovered. Trade-level P&L only."""
        pls = [r["net_pl"] for r in rows
               if pred(r) and not r["protected"] and r["net_pl"] is not None]
        if not pls:
            return {"n": 0, "win_rate": None, "net": None}
        return {"n": len(pls),
                "win_rate": round(sum(1 for p in pls if p > 0) / len(pls) * 100, 1),
                "net": round(sum(pls), 2)}

    return {
        "total": total, "matched": matched,
        "protected": protected, "evaluable": evaluable,
        "uncomparable": uncomparable, "comparable": comparable,
        "protected_reasons": dict(protected_reasons),
        # Denominator is `comparable`: an unclaimed signal has no outcome to be
        # scored against, so counting it would understate the rate as surely as
        # hiding it overstated the sample (#172).
        "match_rate": round(matched / comparable * 100, 1) if comparable else None,
        "claim_coverage": round(comparable / evaluable * 100, 1) if evaluable else None,
        "claimed_outcome": _cohort(lambda r: not r["uncomparable"]),
        "unclaimed_outcome": _cohort(lambda r: r["uncomparable"]),
        "linker": await _linker_health(db),
        "categories": dict(cats),
        "by_source": sorted(by_source.values(),
                            key=lambda x: -(x["total"] + x["protected"] + x["uncomparable"])),
    }


@router.get("")
async def list_rows(date_from: str = None, date_to: str = None, source_id: int = None,
                    category: str = None, include_history: bool = False,
                    limit: int = 300, db: AsyncSession = Depends(get_db)):
    await _link_claims_logged(db)
    rows = await _build_rows(db, _parse_dt(date_from), _parse_dt(date_to),
                             source_id, include_history)
    if category:
        rows = [r for r in rows if r["category"] == category]
    rows.sort(key=lambda r: r["created_at"] or "", reverse=True)
    return rows[:limit]
