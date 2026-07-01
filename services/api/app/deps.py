from typing import AsyncIterator
from sqlalchemy.ext.asyncio import AsyncSession
from beacon_core.db.base import Session


async def get_db() -> AsyncIterator[AsyncSession]:
    async with Session()() as session:
        yield session
