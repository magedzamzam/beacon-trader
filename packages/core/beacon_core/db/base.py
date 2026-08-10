from __future__ import annotations

from sqlalchemy.ext.asyncio import (AsyncSession, async_sessionmaker,
                                     create_async_engine)
from sqlalchemy.orm import DeclarativeBase

from ..config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_Session: async_sessionmaker | None = None


# Additive column migrations, applied on every startup after create_all.
# create_all makes new TABLES but never adds COLUMNS to an existing one, so any
# column added to an already-deployed table must be listed here too (CLAUDE.md
# §6). Each is idempotent (Postgres IF NOT EXISTS) and swallowed on non-Postgres.
# When you add a mapped column to an EXISTING table, add its ALTER here or trade
# INSERTs break on the live box (#112).
ADDITIVE_MIGRATIONS: tuple[str, ...] = (
    "ALTER TABLE telegram_messages "
    "ADD COLUMN IF NOT EXISTS reply_to_message_id INTEGER",
    "ALTER TABLE sources "
    "ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE signals "
    "ADD COLUMN IF NOT EXISTS reinitiated_from INTEGER",   # re-initiate clone link (#66)
    # Signal provenance (#192): `created_at` is INGEST time, so an imported
    # backlog shares one moment. `signal_at` carries the source's own time where
    # it has one; `backfilled` marks history the account never traded, so a P&L
    # replay can exclude it. NOT NULL DEFAULT FALSE — existing rows read as live
    # until the operator marks the known bursts.
    "ALTER TABLE signals "
    "ADD COLUMN IF NOT EXISTS signal_at TIMESTAMPTZ",
    "ALTER TABLE signals "
    "ADD COLUMN IF NOT EXISTS backfilled BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE signal_claims "
    "ADD COLUMN IF NOT EXISTS claim_confidence numeric(4,3)",  # claim link confidence (#63)
    "ALTER TABLE trades "
    "ADD COLUMN IF NOT EXISTS sl_rules JSON",              # point-in-time exit-rules snapshot (#83)
    "ALTER TABLE trades "
    "ADD COLUMN IF NOT EXISTS strategy_id INTEGER",        # ExecutionStrategy attribution (#84)
    "ALTER TABLE trades "
    "ADD COLUMN IF NOT EXISTS deployed_risk numeric(18,6)",  # risk the FILLS put on (#188)
    "ALTER TABLE trades "
    "ADD COLUMN IF NOT EXISTS cluster_id VARCHAR(48)",     # correlation-cluster tag (#106)
    "ALTER TABLE trades "
    "ADD COLUMN IF NOT EXISTS cluster_alloc JSON",         # cluster budgeter record (#106)
    "ALTER TABLE trades "
    "ADD COLUMN IF NOT EXISTS max_favorable_price numeric(18,6)",  # MFE ratchet latch (#149)
    "ALTER TABLE trades "
    "ADD COLUMN IF NOT EXISTS entry_style VARCHAR(16)",    # staged|single_shot, as RUN (#156)
    # cluster_id is index=True in the model, so create_all builds this index on a
    # FRESH table; add it explicitly for the existing-table path (IF NOT EXISTS
    # keeps it a no-op elsewhere — no double CREATE).
    "CREATE INDEX IF NOT EXISTS ix_trades_cluster_id ON trades (cluster_id)",
    # Operator outcome override on an existing table (#136, models.py override_*);
    # the model comment flagged the ALTER but it was omitted → reads AND writes 500
    # on the pre-existing Postgres box (#138). Types mirror the model.
    "ALTER TABLE signal_claims "
    "ADD COLUMN IF NOT EXISTS override_outcome VARCHAR(16)",   # String(16) (#136)
    "ALTER TABLE signal_claims "
    "ADD COLUMN IF NOT EXISTS override_note TEXT",             # Text (#136)
    "ALTER TABLE signal_claims "
    "ADD COLUMN IF NOT EXISTS override_at TIMESTAMPTZ",        # DateTime(tz=True) (#136)
    # Dealing-range low/high added to the existing market_structure table
    # (#113/#137, models.py range_low/range_high) — same missed-ALTER as above (#138).
    "ALTER TABLE market_structure "
    "ADD COLUMN IF NOT EXISTS range_low NUMERIC(18, 6)",       # NUM = Numeric(18,6) (#113/#137)
    "ALTER TABLE market_structure "
    "ADD COLUMN IF NOT EXISTS range_high NUMERIC(18, 6)",      # ^ (#113/#137)
    # Rule epoch on the existing execution_strategies table (#200). Both nullable
    # with no default: NULL means "never stamped", which is the honest state for
    # every row that predates this deploy — the API stamps a row the first time it
    # is written, and the backfill below seeds `epoch_started_at` from the
    # `updated_at` that is currently the only record of an epoch boundary. Filling
    # them with now() instead would claim every live epoch started at deploy time
    # and silently reset Arm B's accumulation, which is the exact bug.
    "ALTER TABLE execution_strategies "
    "ADD COLUMN IF NOT EXISTS epoch_digest VARCHAR(40)",       # String(40) (#200)
    "ALTER TABLE execution_strategies "
    "ADD COLUMN IF NOT EXISTS epoch_started_at TIMESTAMPTZ",   # DateTime(tz) (#200)
)

# One-off data backfills, applied on startup AFTER the ALTERs above. Kept apart
# from ADDITIVE_MIGRATIONS because those are DDL and carry `IF NOT EXISTS`; these
# are DML and must be self-limiting instead — each one has a WHERE clause that
# matches nothing on a second run, so re-running a backfill is a no-op rather
# than an overwrite. `test_backfills_are_self_limiting` pins that.
STARTUP_BACKFILLS: tuple[str, ...] = (
    # #200: seed the epoch clock from the ONLY record of an epoch boundary that
    # exists today. Filling it with now() instead would claim every live epoch
    # began at deploy time and silently reset the accumulations this column was
    # added to protect — on Arm B that is 25 decisive removals, 5 short of its
    # first verdict in three epochs.
    "UPDATE execution_strategies SET epoch_started_at = updated_at "
    "WHERE epoch_started_at IS NULL",
)


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
        for stmt in ADDITIVE_MIGRATIONS:
            try:
                await conn.exec_driver_sql(stmt)
            except Exception:                       # non-Postgres / already applied
                pass
        # After the columns exist, never before.
        for stmt in STARTUP_BACKFILLS:
            try:
                await conn.exec_driver_sql(stmt)
            except Exception:                       # column absent / nothing to do
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
