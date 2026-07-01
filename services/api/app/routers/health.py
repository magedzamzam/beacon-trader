import time
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.bus import Bus
from ..deps import get_db

router = APIRouter(tags=["health"])
bus = Bus()
WORKERS = ("executor", "monitor", "telegram")
STALE_SEC = 30


@router.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    checks = {}
    try:
        await db.execute(text("SELECT 1")); checks["database"] = {"ok": True}
    except Exception as e:
        checks["database"] = {"ok": False, "error": str(e)[:120]}
    try:
        await bus.r.ping(); checks["redis"] = {"ok": True}
    except Exception as e:
        checks["redis"] = {"ok": False, "error": str(e)[:120]}
    now = int(time.time())
    for w in WORKERS:
        beat = await bus.last_beat(w)
        checks[w] = {"ok": bool(beat) and (now - beat) < STALE_SEC,
                     "age_sec": (now - beat) if beat else None}
    overall = all(v.get("ok") for v in checks.values())
    return {"ok": overall, "services": checks}
