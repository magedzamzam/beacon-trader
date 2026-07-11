import asyncio
import time
from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.bus import Bus
from beacon_core.db.models import Broker
from beacon_core.brokers import make_adapter
from ..deps import get_db

router = APIRouter(tags=["health"])
bus = Bus()
WORKERS = ("executor", "monitor", "telegram")
STALE_SEC = 30
BROKER_PROBE_TIMEOUT = 5      # never let a hung broker stall the health poll


async def _broker_health(db) -> dict:
    """Per-broker connectivity + round-trip latency. Reported separately from
    the internal-service checks and NOT folded into `overall` — a demo broker
    being down must not flip the whole system to degraded (#45)."""
    out = {}
    brokers = (await db.execute(
        select(Broker).where(Broker.enabled == True))).scalars().all()
    for b in brokers:
        _a = make_adapter(b)
        try:
            out[b.name] = await asyncio.wait_for(
                _a.healthcheck(), timeout=BROKER_PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            out[b.name] = {"ok": False, "message": f"timeout after {BROKER_PROBE_TIMEOUT}s"}
        except Exception as e:
            out[b.name] = {"ok": False, "message": str(e)[:120]}
        finally:
            try:
                await _a.aclose()
            except Exception:
                pass
    return out


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
    # `overall` covers internal services only (see _broker_health).
    overall = all(v.get("ok") for v in checks.values())
    try:
        brokers = await _broker_health(db)
    except Exception as e:
        brokers = {"_error": {"ok": False, "message": str(e)[:120]}}
    return {"ok": overall, "services": checks, "brokers": brokers}
