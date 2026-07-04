from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.ta import registry as ta_registry
from beacon_core.settings_store import get_setting, set_setting
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/ta", tags=["ta"], dependencies=[Depends(require_token)])

TA_KEY = "ta"


@router.get("/catalog")
async def catalog():
    """Every available indicator (id, label, category, param schema) + the
    selectable timeframes. Drives the portal's indicator picker — the set is not
    hardcoded in the frontend."""
    return ta_registry.catalog()


@router.get("/config")
async def get_config(db: AsyncSession = Depends(get_db)):
    stored = await get_setting(db, TA_KEY, None)
    return ta_registry.sanitize_config(stored) if stored else ta_registry.DEFAULT_CONFIG


@router.put("/config")
async def put_config(body: dict, db: AsyncSession = Depends(get_db)):
    """Save which indicators/params/timeframes to capture per signal. Sanitized
    against the registry (unknown ids dropped, params clamped)."""
    cfg = ta_registry.sanitize_config(body)
    await set_setting(db, TA_KEY, cfg)
    return cfg
