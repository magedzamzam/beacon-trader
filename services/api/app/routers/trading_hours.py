from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.trading_hours import service as th
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/trading-hours", tags=["trading-hours"],
                   dependencies=[Depends(require_token)])


@router.get("/status")
async def get_status(db: AsyncSession = Depends(get_db)):
    """Live session / news-blackout / holiday status (read-only intelligence)."""
    return await th.status(db)


@router.get("/config")
async def get_config(db: AsyncSession = Depends(get_db)):
    return await th.load_config(db)


@router.put("/config")
async def put_config(body: dict, db: AsyncSession = Depends(get_db)):
    return await th.save_config(db, body)


@router.post("/calendar/refresh")
async def refresh_calendar(db: AsyncSession = Depends(get_db)):
    n = await th.refresh_now(db)
    return {"ok": True, "events": n}
