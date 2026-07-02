from __future__ import annotations

from sqlalchemy.ext.asyncio import (AsyncSession, async_sessionmaker,
                                     create_async_engine)
from sqlalchemy.orm import DeclarativeBase

from ..config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_Session: async_sessionmaker | None = None


def engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url,
                                      pool_pre_ping=True, pool_size=5,
                                      max_overflow=10)
    return _engine


def Session() -> async_sessionmaker[AsyncSession]:
    global _Session
    if _Session is None:
        _Session = async_sessionmaker(engine(), expire_on_commit=False,
                                      class_=AsyncSession)
    return _Session


async def init_models() -> None:
    """Create tables if absent. Idempotent; safe to call on startup.

    Import the models module so every table is registered on Base.metadata
    before create_all runs — otherwise a caller that only imported a subset
    (or none) of the models would create an incomplete schema."""
    from . import models  # noqa: F401  (populates Base.metadata)

    async with engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
