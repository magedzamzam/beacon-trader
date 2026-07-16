"""Per-(source, account) execution-policy overrides (#83) — the config surface for
the parallel exit-rule A/B. CRUD over `source_account_policies`; the executor
snapshots the resolved sl_rules onto each trade at entry, so edits here only
affect FUTURE trades (running A/B arms stay frozen).

Phase 1 exposes `sl_rules` + `entry_ttl_minutes`; `entry_policy` is accepted and
stored for the Phase-2 entry A/B but not yet consumed by the executor."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.db.models import Account, Source, SourceAccountPolicy, Trade
from beacon_core.timeutil import utcnow
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/source-account-policies", tags=["ab-policies"],
                   dependencies=[Depends(require_token)])

_VALID_TARGETS = {"entry", "previous_tp", "tp", "number"}
_VALID_TRIGGERS = {"tp_hit", "price_move"}


def _valid_sl_rules(rules) -> bool:
    """Shape-check an sl_rules array against the engine's schema (strategy/rules).
    Not a deep validator — guards against obviously malformed A/B configs that
    would silently no-op in the monitor."""
    if rules is None:
        return True                         # null == 'no override', allowed
    if not isinstance(rules, list):
        return False
    for r in rules:
        if not isinstance(r, dict):
            return False
        trig, act = r.get("trigger"), r.get("action")
        if not isinstance(trig, dict) or trig.get("type") not in _VALID_TRIGGERS:
            return False
        if not isinstance(act, dict) or act.get("type") != "move_sl_to" \
                or act.get("target") not in _VALID_TARGETS:
            return False
    return True


def _shape(p: SourceAccountPolicy) -> dict:
    return {"id": p.id, "source_id": p.source_id, "account_id": p.account_id,
            "sl_rules": p.sl_rules, "entry_ttl_minutes": p.entry_ttl_minutes,
            "entry_policy": p.entry_policy, "enabled": p.enabled,
            "label": p.label, "note": p.note, "version": p.version,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None}


@router.get("")
async def list_policies(source_id: int | None = None, account_id: int | None = None,
                        db: AsyncSession = Depends(get_db)):
    """All overrides, optionally filtered by source and/or account."""
    q = select(SourceAccountPolicy)
    if source_id is not None:
        q = q.where(SourceAccountPolicy.source_id == source_id)
    if account_id is not None:
        q = q.where(SourceAccountPolicy.account_id == account_id)
    rows = (await db.execute(q.order_by(SourceAccountPolicy.source_id,
                                        SourceAccountPolicy.account_id))).scalars().all()
    return [_shape(p) for p in rows]


@router.put("")
async def upsert_policy(body: dict, db: AsyncSession = Depends(get_db)):
    """Create or update the override for one (source, account). Bumps `version` on
    every edit so trades can be attributed to the exact arm they ran under."""
    try:
        source_id = int(body["source_id"])
        account_id = int(body["account_id"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, "source_id and account_id are required integers")
    if not await db.get(Source, source_id):
        raise HTTPException(404, f"source {source_id} not found")
    if not await db.get(Account, account_id):
        raise HTTPException(404, f"account {account_id} not found")

    sl_rules = body.get("sl_rules")
    if not _valid_sl_rules(sl_rules):
        raise HTTPException(422, "sl_rules must be a list of {trigger, action:move_sl_to} rules")
    ttl = body.get("entry_ttl_minutes")
    if ttl is not None:
        try:
            ttl = max(1, min(1440, int(ttl)))
        except (TypeError, ValueError):
            raise HTTPException(422, "entry_ttl_minutes must be an integer")

    existing = (await db.execute(select(SourceAccountPolicy).where(
        SourceAccountPolicy.source_id == source_id,
        SourceAccountPolicy.account_id == account_id))).scalar_one_or_none()
    if existing:
        existing.sl_rules = sl_rules
        existing.entry_ttl_minutes = ttl
        existing.entry_policy = body.get("entry_policy")
        existing.enabled = bool(body.get("enabled", True))
        existing.label = (body.get("label") or None)
        existing.note = (body.get("note") or None)
        existing.version = (existing.version or 1) + 1
        existing.updated_at = utcnow()
        row = existing
    else:
        row = SourceAccountPolicy(
            source_id=source_id, account_id=account_id, sl_rules=sl_rules,
            entry_ttl_minutes=ttl, entry_policy=body.get("entry_policy"),
            enabled=bool(body.get("enabled", True)),
            label=(body.get("label") or None), note=(body.get("note") or None))
        db.add(row)
    await db.commit()
    await db.refresh(row)
    return _shape(row)


@router.delete("/{policy_id}")
async def delete_policy(policy_id: int, db: AsyncSession = Depends(get_db)):
    """Remove an override (future trades revert to the source/global default).
    Existing trades keep their snapshot, so their A/B arm is unaffected."""
    row = await db.get(SourceAccountPolicy, policy_id)
    if not row:
        raise HTTPException(404, "policy not found")
    await db.delete(row)
    await db.commit()
    return {"ok": True, "deleted": policy_id}
