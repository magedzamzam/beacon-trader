"""Migration 001 — Alpha Layer Phase 0e latency stamps.

New TABLES are created automatically by init_models() (Base.metadata.create_all).
New COLUMNS on existing tables are NOT, so this adds them idempotently with
`ADD COLUMN IF NOT EXISTS` (PostgreSQL). Safe to run repeatedly.

    docker compose run --rm api python -m scripts.migrate_001
    # or, from a shell with beacon_core installed:
    python scripts/migrate_001.py
"""
import asyncio

from sqlalchemy import text

from beacon_core.db.base import engine

# (table, column, type) — all nullable, tz-aware timestamps.
_COLUMNS = [
    ("signals", "provider_ts", "TIMESTAMPTZ"),
    ("signals", "received_ts", "TIMESTAMPTZ"),
    ("signals", "published_ts", "TIMESTAMPTZ"),
    ("legs", "submitted_ts", "TIMESTAMPTZ"),
    ("legs", "broker_ack_ts", "TIMESTAMPTZ"),
]


async def main() -> None:
    async with engine().begin() as conn:
        for table, column, coltype in _COLUMNS:
            await conn.execute(text(
                f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}'))
            print(f"ok: {table}.{column}")
    print("migrate_001 complete.")


if __name__ == "__main__":
    asyncio.run(main())
