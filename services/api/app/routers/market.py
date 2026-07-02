from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.db.models import Broker, SymbolMap
from beacon_core.brokers import get_adapter, resolve_credentials
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/market", tags=["market"], dependencies=[Depends(require_token)])


async def _resolve(db, symbol, broker_id):
    if broker_id:
        broker = await db.get(Broker, broker_id)
    else:
        broker = (await db.execute(select(Broker).where(Broker.enabled == True))).scalars().first()
    if not broker:
        raise HTTPException(404, "no enabled broker")
    smap = (await db.execute(select(SymbolMap).where(
        SymbolMap.broker_id == broker.id, SymbolMap.internal_symbol == symbol))).scalar_one_or_none()
    if not smap:
        raise HTTPException(404, f"no symbol map for {symbol}")
    creds = resolve_credentials(broker.credentials_ref); creds.setdefault("is_demo", broker.is_demo)
    return broker, smap, get_adapter(broker.type, creds)


@router.get("/candles")
async def candles(symbol: str = "XAUUSD", resolution: str = "MINUTE_5",
                  max_bars: int = 200, broker_id: int | None = None,
                  db: AsyncSession = Depends(get_db)):
    _, smap, adapter = await _resolve(db, symbol, broker_id)
    try:
        bars = await adapter.get_bars(smap.broker_epic, resolution, max_bars=max_bars)
        return {"symbol": symbol, "epic": smap.broker_epic, "resolution": resolution, "bars": bars}
    except Exception as exc:
        raise HTTPException(502, f"broker candles failed: {exc}")
    finally:
        await adapter.aclose()


@router.get("/quote")
async def quote(symbol: str = "XAUUSD", broker_id: int | None = None,
                db: AsyncSession = Depends(get_db)):
    _, smap, adapter = await _resolve(db, symbol, broker_id)
    try:
        q = await adapter.get_quote(smap.broker_epic)
        return {"symbol": symbol, "bid": float(q.bid) if q.bid else None,
                "offer": float(q.offer) if q.offer else None,
                "last": float(q.last_price) if q.last_price else None,
                "currency": q.currency, "status": q.market_status}
    except Exception as exc:
        raise HTTPException(502, f"broker quote failed: {exc}")
    finally:
        await adapter.aclose()
