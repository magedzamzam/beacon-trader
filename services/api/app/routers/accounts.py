from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.db.models import Account, Broker, Leg, Trade
from beacon_core.brokers import make_adapter
from ..deps import get_db
from ..auth import require_token
from ..schemas import AccountIn

router = APIRouter(prefix="/accounts", tags=["accounts"], dependencies=[Depends(require_token)])


@router.get("")
async def list_accounts(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Account))).scalars().all()
    return [{"id": a.id, "broker_id": a.broker_id, "name": a.name,
             "broker_account_id": a.broker_account_id, "currency": a.currency,
             "enabled": a.enabled, "risk_config": a.risk_config} for a in rows]


@router.get("/{account_id}/performance")
async def account_performance(account_id: int, db: AsyncSession = Depends(get_db)):
    """Per-account performance card: realized P&L, win rate and open exposure
    from Beacon's ledger, plus live balance/equity fetched best-effort from the
    broker. Broker figures are null if the broker can't be reached — the DB
    metrics are always returned."""
    acct = await db.get(Account, account_id)
    if not acct:
        raise HTTPException(404, "account not found")

    # --- DB metrics (always available) ---
    realized_pl = float((await db.execute(select(func.coalesce(func.sum(Trade.realized_pl), 0))
                                          .where(Trade.account_id == account_id))).scalar() or 0)
    total_trades = (await db.execute(select(func.count(Trade.id))
                                     .where(Trade.account_id == account_id))).scalar() or 0
    open_trades = (await db.execute(select(func.count(Trade.id)).where(
        Trade.account_id == account_id, Trade.status.in_(("open", "partial"))))).scalar() or 0
    open_legs = (await db.execute(select(func.count(Leg.id))
                                  .join(Trade, Trade.id == Leg.trade_id)
                                  .where(Trade.account_id == account_id,
                                         Leg.status.in_(("open", "working", "pending"))))).scalar() or 0
    closed = (await db.execute(select(func.count(Leg.id))
                               .join(Trade, Trade.id == Leg.trade_id)
                               .where(Trade.account_id == account_id, Leg.status == "closed"))).scalar() or 0
    wins = (await db.execute(select(func.count(Leg.id))
                             .join(Trade, Trade.id == Leg.trade_id)
                             .where(Trade.account_id == account_id, Leg.outcome == "tp_hit"))).scalar() or 0
    win_rate = round(wins / closed * 100.0, 2) if closed else 0.0

    # --- live balance/equity (best-effort) ---
    balance = available = None
    try:
        b = await db.get(Broker, acct.broker_id)
        if b:
            adapter = make_adapter(b)
            try:
                live = await adapter.list_accounts()
            finally:
                await adapter.aclose()
            match = next((x for x in live
                          if str(x.get("broker_account_id")) == str(acct.broker_account_id)), None)
            if match:
                balance = float(match["balance"]) if match.get("balance") else None
                available = float(match["available"]) if match.get("available") else None
    except Exception:
        pass  # broker unreachable — DB metrics still returned

    # PL% relative to inferred starting balance (current balance minus realized).
    pl_pct = None
    if balance is not None:
        start = balance - realized_pl
        if abs(start) > 1e-9:
            pl_pct = round(realized_pl / start * 100.0, 2)

    return {"account_id": account_id, "name": acct.name, "currency": acct.currency,
            "realized_pl": round(realized_pl, 2), "pl_pct": pl_pct,
            "balance": balance, "equity": balance, "available": available,
            "win_rate": win_rate, "closed_legs": closed,
            "open_legs": open_legs, "open_trades": open_trades, "total_trades": total_trades}


@router.post("")
async def create_account(body: AccountIn, db: AsyncSession = Depends(get_db)):
    a = Account(**body.model_dump()); db.add(a); await db.commit()
    return {"id": a.id}


@router.patch("/{account_id}")
async def update_account(account_id: int, body: dict, db: AsyncSession = Depends(get_db)):
    a = await db.get(Account, account_id)
    for k in ("name", "currency", "enabled", "risk_config"):
        if k in body:
            setattr(a, k, body[k])
    await db.commit()
    return {"ok": True}


@router.delete("/{account_id}")
async def delete_account(account_id: int, db: AsyncSession = Depends(get_db)):
    a = await db.get(Account, account_id)
    if a:
        await db.delete(a); await db.commit()
    return {"ok": True}
