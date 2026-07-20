"""Startup additive-migration coverage (#112).

`create_all` builds new *tables* but never adds *columns* to an existing one
(CLAUDE.md §6), so any column mapped on an already-deployed table must also have
an `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` entry in `ADDITIVE_MIGRATIONS`.
#106 added `cluster_id`/`cluster_alloc` to the *existing* `trades` table but
forgot the ALTERs, so on the live box (pre-existing `trades`) the columns never
appeared and every trade INSERT — which writes them unconditionally — failed.
These tests pin that the columns the executor writes are covered on deploy.
"""
import re

from beacon_core.db import base as B
from beacon_core.db.models import Trade


def _added_columns(table: str) -> set[str]:
    """Column names an ADD COLUMN migration creates for `table`."""
    pat = re.compile(
        r"ALTER\s+TABLE\s+" + re.escape(table)
        + r"\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)", re.IGNORECASE)
    out = set()
    for stmt in B.ADDITIVE_MIGRATIONS:
        m = pat.search(stmt)
        if m:
            out.add(m.group(1).lower())
    return out


def test_cluster_columns_have_startup_alters():
    # The exact regression from #112: both cluster columns must be in the list.
    trades_added = _added_columns("trades")
    assert "cluster_id" in trades_added
    assert "cluster_alloc" in trades_added


def test_cluster_index_created_for_existing_table():
    # cluster_id is index=True in the model; the existing-table path needs an
    # explicit CREATE INDEX (create_all only indexes it on a fresh table).
    assert any(
        re.search(r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+ix_trades_cluster_id",
                  s, re.IGNORECASE)
        for s in B.ADDITIVE_MIGRATIONS)


def test_migrations_are_idempotent():
    # Every entry must be safe to re-run on startup.
    for stmt in B.ADDITIVE_MIGRATIONS:
        assert "IF NOT EXISTS" in stmt.upper(), stmt


def test_model_and_migration_agree_on_cluster_columns():
    # Guard against drift: if the model maps these, the migration must add them.
    cols = set(Trade.__table__.columns.keys())
    assert {"cluster_id", "cluster_alloc"} <= cols
    trades_added = _added_columns("trades")
    assert {"cluster_id", "cluster_alloc"} <= trades_added
