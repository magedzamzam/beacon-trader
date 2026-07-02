"""Async helpers for the DB-backed Setting key/value store.

Runtime configuration the operator can change from the UI lives here (AI
provider config, feature toggles) so the platform is reconfigurable without a
redeploy. Values are plain JSON; secrets inside them are encrypted with
beacon_core.crypto before they are written.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Setting


async def get_setting(session: AsyncSession, key: str, default: Any = None) -> Any:
    row = await session.get(Setting, key)
    return row.value if row is not None else default


async def set_setting(session: AsyncSession, key: str, value: dict) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    await session.commit()
