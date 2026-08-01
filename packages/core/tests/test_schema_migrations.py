"""Startup additive-migration coverage (#112, #138).

`create_all` builds new *tables* but never adds *columns* to an existing one
(CLAUDE.md §6), so any column mapped on an already-deployed table must also have
an `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` entry in `ADDITIVE_MIGRATIONS`.
#106 added `cluster_id`/`cluster_alloc` to the *existing* `trades` table but
forgot the ALTERs, so on the live box (pre-existing `trades`) the columns never
appeared and every trade INSERT — which writes them unconditionally — failed.
The `trades` tests below pin that regression.

#138 was the same defect class recurring: `signal_claims` (#136) and
`market_structure` (#113/#137) each gained mapped columns with no ALTER, which
breaks reads too (ORM `select(Model)` emits every mapped column). CI stayed green
because SQLite `create_all` rebuilds every column each run — the break only shows
against a pre-existing Postgres schema. `test_no_uncovered_column_drift` below is
the generic guard: for a curated set of long-lived tables it asserts every mapped
column is either in the table's frozen creation-time baseline or added by an
ALTER — so the next missed column fails CI instead of the live box.
"""
import re

from beacon_core.db import base as B
from beacon_core.db.base import Base
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


def test_signal_claims_override_columns_have_alters():
    # #136/#138: the operator-override columns must be covered on deploy.
    added = _added_columns("signal_claims")
    assert {"override_outcome", "override_note", "override_at"} <= added


def test_signal_provenance_columns_have_alters():
    # #192: `signals` is long-lived, so the provenance columns need ALTERs or the
    # replay loader's `backfilled IS NOT true` filter 500s on the live box.
    added = _added_columns("signals")
    assert {"signal_at", "backfilled"} <= added


def test_backfilled_defaults_to_false_on_existing_rows():
    # The 856 rows already there predate the flag. The ALTER must give them a
    # value — a NULL-filled column would leave "is this history?" unanswerable
    # for the whole book, which is the bug this column exists to end.
    stmt = next(s for s in B.ADDITIVE_MIGRATIONS
                if re.search(r"ALTER\s+TABLE\s+signals\b.*\bbackfilled\b", s,
                             re.IGNORECASE))
    assert re.search(r"DEFAULT\s+FALSE", stmt, re.IGNORECASE), stmt


def test_market_structure_range_columns_have_alters():
    # #113/#137/#138: the dealing-range columns must be covered on deploy.
    added = _added_columns("market_structure")
    assert {"range_low", "range_high"} <= added


# ---------------------------------------------------------------------------
# Generic drift guard (#138)
# ---------------------------------------------------------------------------
# For each long-lived table, BASELINE_COLUMNS freezes the columns it was
# *originally created with* — the schema `create_all` built on the first deploy,
# before any additive migration. It is deliberately hand-maintained and MUST NOT
# be edited to absorb a newly mapped column: a new column belongs in an ALTER in
# ADDITIVE_MIGRATIONS, not in this baseline. If a column mapped on one of these
# tables is neither in the baseline nor added by an ALTER, it will silently be
# absent on the pre-existing Postgres box (CLAUDE.md §6) and 500 every read/write
# that touches it — exactly the #112/#138 failure. This test makes that fail in
# CI instead. When you legitimately add a column to one of these tables: add its
# ALTER (do NOT touch the baseline below).
BASELINE_COLUMNS: dict[str, frozenset[str]] = {
    "trades": frozenset({
        "account_id", "created_at", "direction", "id", "planned_risk",
        "realized_pl", "signal_id", "status", "symbol",
    }),
    "signals": frozenset({
        "created_at", "dedupe_hash", "direction", "entry_from", "entry_to",
        "id", "market_snapshot", "order_type", "raw_text", "reject_reason",
        "sl", "source_id", "status", "symbol", "tps",
    }),
    "signal_claims": frozenset({
        "all_tp", "claimed_at", "created_at", "id", "max_tp_claimed",
        "message_id", "raw_text", "signal_id", "sl_claimed", "source_id",
    }),
    "market_structure": frozenset({
        "active", "atr", "bias_price", "computed_at", "id", "label",
        "last_event", "premium_discount", "superseded_at", "swings", "symbol",
        "timeframe", "version_id",
    }),
    "sources": frozenset({
        "account_map", "created_at", "enabled_for_trading", "external_id",
        "id", "is_trusted", "kind", "name", "risk_config", "strategy",
    }),
    "telegram_messages": frozenset({
        "chat_id", "created_at", "id", "is_signal", "message_date",
        "message_id", "parse_status", "reject_reason", "sender", "signal_id",
        "source_id", "text",
    }),
}


def test_baseline_tables_exist_in_metadata():
    # Catch a curated table being renamed/dropped out from under this guard.
    for table in BASELINE_COLUMNS:
        assert table in Base.metadata.tables, table


def test_no_uncovered_column_drift():
    # Every mapped column on a curated long-lived table must be covered either by
    # its frozen creation-time baseline or by an ADD COLUMN migration.
    for table, baseline in BASELINE_COLUMNS.items():
        mapped = {c.name for c in Base.metadata.tables[table].columns}
        covered = baseline | _added_columns(table)
        uncovered = mapped - covered
        assert not uncovered, (
            f"{table}: mapped column(s) {sorted(uncovered)} have no startup "
            f"ALTER in ADDITIVE_MIGRATIONS (and are not in the frozen baseline). "
            f"Add an 'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS ...' — do not "
            f"edit BASELINE_COLUMNS (CLAUDE.md §6, #112/#138)."
        )
