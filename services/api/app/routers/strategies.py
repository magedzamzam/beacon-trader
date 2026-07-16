"""Execution strategies (#84) — the config surface for per-(account, source)
Entry / Filtration / Exit policy. CRUD over `execution_strategies`.

Scope is (account_id, source_id), both nullable: null = "any". Most-specific
enabled scope wins at resolution time. The executor snapshots the resolved exit
rules onto each trade at entry, so edits here only affect FUTURE trades — running
A/B arms stay frozen (clean attribution). Risk is NOT here (Risk & Limits owns it)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.db.models import Account, ExecutionStrategy, Source
from beacon_core.execution import strategy as ST
from beacon_core.execution.strategy import ENTRY_POLICY_KEYS
from beacon_core.timeutil import utcnow
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/strategies", tags=["strategies"],
                   dependencies=[Depends(require_token)])

_SL_TARGETS = {"entry", "previous_tp", "tp", "number"}
_SL_TRIGGERS = {"tp_hit", "price_move"}
_FILTER_WHEN = {"always", "session_in"}       # extensible; see execution/strategy.apply_filter_rules
_FILTER_ACTIONS = {"skip", "scale"}


def _valid_sl_rules(rules) -> bool:
    if rules is None:
        return True
    if not isinstance(rules, list):
        return False
    for r in rules:
        if not isinstance(r, dict):
            return False
        t, a = r.get("trigger"), r.get("action")
        if not isinstance(t, dict) or t.get("type") not in _SL_TRIGGERS:
            return False
        if not isinstance(a, dict) or a.get("type") != "move_sl_to" or a.get("target") not in _SL_TARGETS:
            return False
    return True


def _clean_entry_policy(ep) -> dict | None:
    """Keep only known entry-policy keys (#67 chase guard + TTL)."""
    if ep is None:
        return None
    if not isinstance(ep, dict):
        raise HTTPException(422, "entry_policy must be an object")
    return {k: ep[k] for k in ENTRY_POLICY_KEYS if k in ep and ep[k] is not None} or None


def _clean_entry_filters(ef) -> dict | None:
    if ef is None:
        return None
    if not isinstance(ef, dict):
        raise HTTPException(422, "entry_filters must be an object")
    rules = ef.get("rules")
    if rules is not None:
        if not isinstance(rules, list):
            raise HTTPException(422, "entry_filters.rules must be a list")
        for r in rules:
            if not isinstance(r, dict) or (r.get("when") or {}).get("type") not in _FILTER_WHEN \
                    or r.get("action") not in _FILTER_ACTIONS:
                raise HTTPException(422, "each filter rule needs a known when.type and action")
    return ef or None


def _clean_exit_policy(xp) -> dict | None:
    if xp is None:
        return None
    if not isinstance(xp, dict):
        raise HTTPException(422, "exit_policy must be an object")
    if not _valid_sl_rules(xp.get("sl_rules")):
        raise HTTPException(422, "exit_policy.sl_rules must be a list of {trigger, action:move_sl_to}")
    return xp or None


def _shape(s: ExecutionStrategy) -> dict:
    return {"id": s.id, "account_id": s.account_id, "source_id": s.source_id,
            "entry_policy": s.entry_policy, "entry_filters": s.entry_filters,
            "exit_policy": s.exit_policy, "enabled": s.enabled, "label": s.label,
            "note": s.note, "version": s.version,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None}


@router.get("")
async def list_strategies(account_id: int | None = None, source_id: int | None = None,
                          db: AsyncSession = Depends(get_db)):
    """All strategies, optionally filtered to an exact account and/or source scope."""
    q = select(ExecutionStrategy)
    if account_id is not None:
        q = q.where(ExecutionStrategy.account_id == account_id)
    if source_id is not None:
        q = q.where(ExecutionStrategy.source_id == source_id)
    rows = (await db.execute(q.order_by(ExecutionStrategy.account_id.nulls_last(),
                                        ExecutionStrategy.source_id.nulls_last()))).scalars().all()
    return [_shape(s) for s in rows]


@router.get("/resolve")
async def resolve(account_id: int, source_id: int, db: AsyncSession = Depends(get_db)):
    """Preview which strategy (and pillars) a trade on (account, source) would run
    under — the most-specific enabled match, or null if none (global defaults apply)."""
    rows = (await db.execute(select(ExecutionStrategy))).scalars().all()
    s = ST.resolve_strategy(rows, account_id, source_id)
    return {"resolved": _shape(s) if s else None}


@router.put("")
async def upsert(body: dict, db: AsyncSession = Depends(get_db)):
    """Create/update the strategy for one scope. account_id/source_id may be null
    (= any). Bumps `version` on every edit for trade attribution."""
    def _scope(key):
        v = body.get(key)
        if v in (None, "", "any"):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            raise HTTPException(422, f"{key} must be an integer or null")
    account_id, source_id = _scope("account_id"), _scope("source_id")
    if account_id is not None and not await db.get(Account, account_id):
        raise HTTPException(404, f"account {account_id} not found")
    if source_id is not None and not await db.get(Source, source_id):
        raise HTTPException(404, f"source {source_id} not found")

    entry_policy = _clean_entry_policy(body.get("entry_policy"))
    entry_filters = _clean_entry_filters(body.get("entry_filters"))
    exit_policy = _clean_exit_policy(body.get("exit_policy"))

    existing = (await db.execute(select(ExecutionStrategy).where(
        ExecutionStrategy.account_id.is_(account_id) if account_id is None
        else ExecutionStrategy.account_id == account_id,
        ExecutionStrategy.source_id.is_(source_id) if source_id is None
        else ExecutionStrategy.source_id == source_id))).scalar_one_or_none()
    if existing:
        existing.entry_policy = entry_policy
        existing.entry_filters = entry_filters
        existing.exit_policy = exit_policy
        existing.enabled = bool(body.get("enabled", True))
        existing.label = (body.get("label") or None)
        existing.note = (body.get("note") or None)
        existing.version = (existing.version or 1) + 1
        existing.updated_at = utcnow()
        row = existing
    else:
        row = ExecutionStrategy(
            account_id=account_id, source_id=source_id, entry_policy=entry_policy,
            entry_filters=entry_filters, exit_policy=exit_policy,
            enabled=bool(body.get("enabled", True)),
            label=(body.get("label") or None), note=(body.get("note") or None))
        db.add(row)
    await db.commit()
    await db.refresh(row)
    return _shape(row)


@router.delete("/{strategy_id}")
async def delete(strategy_id: int, db: AsyncSession = Depends(get_db)):
    """Remove a strategy (future trades revert to the next-specific scope / default).
    Existing trades keep their exit snapshot, so their A/B arm is unaffected."""
    row = await db.get(ExecutionStrategy, strategy_id)
    if not row:
        raise HTTPException(404, "strategy not found")
    await db.delete(row)
    await db.commit()
    return {"ok": True, "deleted": strategy_id}
