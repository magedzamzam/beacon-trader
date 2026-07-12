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
BROKER_PROBE_TIMEOUT = 8      # room for one 429 /session retry on a cold login
# Each broker probe used to be a FRESH Capital.com /session login, which
# Capital.com strictly rate-limits (~1/sec per API key). With two brokers (e.g.
# Live + Demo) probed back-to-back the second login 429'd and, wrapped in the
# probe timeout, showed as "down" — while Test Connection (one broker, no
# timeout) succeeded. Fix: (1) PERSIST a logged-in adapter per broker and reuse
# it across probes (re-login only on session expiry/failure — self-healing, and
# each broker's expiry is staggered, so they rarely collide), and (2) TTL-cache
# the whole result so many pollers/tabs share one refresh per minute.
BROKER_CACHE_TTL = 60
_broker_cache = {"ts": 0.0, "data": None}
_broker_lock = asyncio.Lock()
# broker_id -> logged-in adapter, reused across probes (each isolated: own
# host/credentials/session tokens — no cross-referencing between brokers).
_broker_adapters: dict = {}


async def _evict_adapter(broker_id: int) -> None:
    a = _broker_adapters.pop(broker_id, None)
    if a is not None:
        try:
            await a.aclose()
        except Exception:
            pass


async def _probe_brokers(db) -> dict:
    out = {}
    brokers = (await db.execute(
        select(Broker).where(Broker.enabled == True))).scalars().all()
    live_ids = {b.id for b in brokers}
    for _bid in [i for i in _broker_adapters if i not in live_ids]:
        await _evict_adapter(_bid)          # drop adapters for disabled/removed brokers

    for b in brokers:
        a = _broker_adapters.get(b.id)
        if a is None:
            a = make_adapter(b)             # constructed now; logs in lazily on first call
            _broker_adapters[b.id] = a
        try:
            res = await asyncio.wait_for(a.healthcheck(), timeout=BROKER_PROBE_TIMEOUT)
            # On failure, drop the adapter so the next cycle rebuilds/re-logins.
            # By then the other broker is cached, so the two don't collide.
            if not res.get("ok"):
                await _evict_adapter(b.id)
        except asyncio.TimeoutError:
            res = {"ok": False, "message": f"timeout after {BROKER_PROBE_TIMEOUT}s"}
            await _evict_adapter(b.id)
        except Exception as e:
            res = {"ok": False, "message": str(e)[:120]}
            await _evict_adapter(b.id)
        out[b.name] = res
    return out


async def _broker_health(db) -> dict:
    """Per-broker connectivity + latency, TTL-cached so many pollers/tabs share
    one Capital.com login per minute. Reported separately from the internal-
    service checks and NOT folded into `overall` (#45)."""
    now = time.monotonic()
    cached = _broker_cache["data"]
    if cached is not None and (now - _broker_cache["ts"]) < BROKER_CACHE_TTL:
        return cached
    # One prober at a time; concurrent pollers wait and reuse the fresh result
    # instead of each firing their own login (thundering-herd guard).
    async with _broker_lock:
        now = time.monotonic()
        cached = _broker_cache["data"]
        if cached is not None and (now - _broker_cache["ts"]) < BROKER_CACHE_TTL:
            return cached
        out = await _probe_brokers(db)
        _broker_cache["data"] = out
        _broker_cache["ts"] = time.monotonic()
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
