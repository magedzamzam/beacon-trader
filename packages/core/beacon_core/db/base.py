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
        # create_all makes new TABLES but never adds COLUMNS to existing ones —
        # self-apply the additive columns (idempotent; Postgres IF NOT EXISTS).
        for stmt in (
            "ALTER TABLE telegram_messages "
            "ADD COLUMN IF NOT EXISTS reply_to_message_id INTEGER",
            "ALTER TABLE sources "
            "ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE signals "
            "ADD COLUMN IF NOT EXISTS reinitiated_from INTEGER",   # re-initiate clone link (#66)
            "ALTER TABLE signal_claims "
            "ADD COLUMN IF NOT EXISTS claim_confidence numeric(4,3)",  # claim link confidence (#63)
            "ALTER TABLE trades "
            "ADD COLUMN IF NOT EXISTS sl_policy_id INTEGER",       # #83 A/B policy attribution
            "ALTER TABLE trades "
            "ADD COLUMN IF NOT EXISTS sl_rules JSON",              # #83 point-in-time sl_rules snapshot
        ):
            try:
                await conn.exec_driver_sql(stmt)
            except Exception:                       # non-Postgres / already applied
                pass

    # Idempotency backstop (#15): at most one trade per (signal, account). The
    # executor already guards this in code (existence check + already-executed
    # short-circuit); this makes a concurrent/retried double-place fail at the
    # DB layer too. Run in its OWN transaction — unlike the IF-NOT-EXISTS ALTERs
    # above, this DDL can legitimately fail if pre-existing duplicates block the
    # unique index, and a failure inside the create_all transaction would poison
    # it. On failure the code guard still protects.
    try:
        async with engine().begin() as conn:
            await conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_signal_account "
                "ON trades (signal_id, account_id)")
    except Exception:
        pass
