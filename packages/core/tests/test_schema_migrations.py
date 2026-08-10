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
the generic guard: for every table it asserts each mapped column is either in the
table's frozen creation-time baseline or added by an ALTER — so the next missed
column fails CI instead of the live box.

#193 made that guard table-complete. It used to cover 6 of 27 tables — the 6 that
had already broken — which left `legs` unguarded: written on every fill and read
whole on every monitor tick, so a missed ALTER there is a Critical live outcome
on the one table that can least afford it. `test_every_table_is_guarded` now
fails closed, so a new table is covered by default and an exclusion is a
deliberate entry in `UNGUARDED_TABLES` rather than a silent omission.
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


def test_backfills_are_self_limiting():
    """The backfills are DML, so `IF NOT EXISTS` cannot protect them — a WHERE
    clause that stops matching after the first run has to. A backfill that
    re-ran unguarded on every startup would overwrite the very thing it was
    added to preserve (#200 seeds an epoch clock from `updated_at`; re-running
    it after a later edit would silently re-date a live accumulation)."""
    for stmt in B.STARTUP_BACKFILLS:
        upper = stmt.upper()
        assert upper.startswith("UPDATE "), stmt
        assert " WHERE " in upper and "IS NULL" in upper, stmt


def test_epoch_columns_have_alters():
    # #200: `execution_strategies` is long-lived, so the epoch pair needs ALTERs
    # or `select(ExecutionStrategy)` 500s on the live box the moment it deploys.
    added = _added_columns("execution_strategies")
    assert {"epoch_digest", "epoch_started_at"} <= added


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
# For each table, BASELINE_COLUMNS freezes the columns it was *originally
# created with* — the schema `create_all` built on the first deploy, before any
# additive migration. It is deliberately hand-maintained and MUST NOT be edited
# to absorb a newly mapped column: a new column belongs in an ALTER in
# ADDITIVE_MIGRATIONS, not in this baseline. If a column mapped on one of these
# tables is neither in the baseline nor added by an ALTER, it will silently be
# absent on the pre-existing Postgres box (CLAUDE.md §6) and 500 every read/write
# that touches it — exactly the #112/#138 failure. This test makes that fail in
# CI instead. When you legitimately add a column to one of these tables: add its
# ALTER (do NOT touch the baseline below).
#
# #193: this dict used to curate 6 of 27 tables — the 6 that had ALREADY broken.
# Everything else was silently unguarded, including `legs`, which is INSERTed on
# every fill (`services/executor/main.py`) and read with a full-model
# `select(Leg)` on every monitor tick (`services/monitor/main.py`), so a missed
# ALTER there would 500 both fill recording and the SL-ratchet loop while CI
# stayed green. It is now TABLE-COMPLETE and fail-closed:
# `test_every_table_is_guarded` refuses any table in `Base.metadata` that is
# neither baselined here nor deliberately listed in `UNGUARDED_TABLES`, so a
# brand-new table is guarded by default and excluding one is a reviewed act.
#
# The 21 baselines added by #193 were frozen from the CURRENT mapped set, which
# is only sound because it was checked against the live schema rather than
# assumed: `beacon_20260809.sql` (26 public tables) plus a read-only
# `information_schema` query for `candles` (excluded from the dump, §7) showed
# **no mapped column missing on the box** for any of the 27 — i.e. there is no
# pre-existing drift being blessed into a baseline here. The only diff was
# `trades.sl_policy_id`, live-only, a column the model dropped.
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
    # --- added by #193: the other 21 tables, previously unguarded -----------
    "account_source_risk": frozenset({
        "account_id", "enabled", "id", "note", "risk_config", "source_id",
        "updated_at",
    }),
    "accounts": frozenset({
        "broker_account_id", "broker_id", "currency", "enabled", "id",
        "name", "risk_config",
    }),
    "ai_assessments": frozenset({
        "account_id", "confidence", "created_at", "id", "kind", "model",
        "payload", "provider", "rationale", "score", "signal_id",
        "trade_id", "verdict",
    }),
    "brokers": frozenset({
        "created_at", "credentials_ref", "enabled", "id", "is_demo", "name",
        "type",
    }),
    "candles": frozenset({
        "close_ask", "close_bid", "high_ask", "high_bid", "id",
        "ingested_at", "low_ask", "low_bid", "open_ask", "open_bid",
        "quality", "source", "spread_nominal", "symbol", "timeframe", "ts",
        "volume",
    }),
    "econ_events": frozenset({
        "ccy", "created_at", "id", "impact", "title", "ts",
    }),
    "events": frozenset({
        "id", "kind", "leg_id", "payload", "trade_id", "ts",
    }),
    "execution_strategies": frozenset({
        "account_id", "created_at", "enabled", "entry_filters",
        "entry_policy", "exit_policy", "id", "label", "note", "source_id",
        "updated_at", "version",
    }),
    # The sharp edge #193 was filed for: written on every fill, read whole on
    # every monitor tick.
    "legs": frozenset({
        "broker_order_ref", "broker_position_ref", "close_price",
        "closed_at", "created_at", "entry", "fill_price", "id", "lot",
        "order_type", "outcome", "realized_pl", "sl", "sl_moved", "status",
        "tp", "tp_index", "trade_id",
    }),
    "magnet_zones": frozenset({
        "active", "computed_at", "id", "members", "mid", "n_timeframes",
        "price_high", "price_low", "rank", "ref_atr", "score",
        "superseded_at", "symbol", "version_id",
    }),
    "notification_deliveries": frozenset({
        "created_at", "event_id", "id", "ok", "results", "subject",
    }),
    "position_activities": frozenset({
        "account_id", "activity_at", "created_at", "currency", "deal_id",
        "deal_reference", "epic", "id", "leg_id", "payload", "realized_pl",
        "source", "status", "trade_id", "type",
    }),
    "settings": frozenset({
        "key", "updated_at", "value",
    }),
    "signal_analytics": frozenset({
        "analytics", "captured_at", "degraded", "direction", "id", "price",
        "regime", "signal_id", "symbol", "window",
    }),
    "signal_excursions": frozenset({
        "bars_to_sl", "bars_to_tp1", "basis", "clock_source", "computed_at",
        "direction", "entry", "entry_at", "horizon_bars", "horizon_capped",
        "id", "ladder", "mae_r", "mfe_r", "n_bars", "r", "race",
        "same_bar_ambiguous", "signal_id", "sl", "symbol", "tp1", "tp1_r",
        "trade_id",
    }),
    "signal_features": frozenset({
        "captured_at", "direction", "features", "id", "price", "session",
        "signal_id", "symbol", "utc_hour",
    }),
    "staged_entries": frozenset({
        "account_id", "atr", "cfg", "created_at", "deep_edge", "direction",
        "id", "max_adverse_beyond_deep", "near_edge", "sl", "trade_id",
        "updated_at",
    }),
    "staged_tranches": frozenset({
        "broker_order_ref", "created_at", "id", "leg_ids", "mode", "reason",
        "role", "state", "state_since", "trade_id", "trigger_level",
    }),
    "structure_levels": frozenset({
        "active", "anchor_a", "anchor_b", "anchor_c", "computed_at",
        "direction", "id", "kind", "price", "ratio", "structure_id",
        "superseded_at", "symbol", "timeframe", "version_id", "weight",
    }),
    "symbol_maps": frozenset({
        "broker_epic", "broker_id", "id", "internal_symbol", "lot_step",
        "min_lot", "min_stop_distance", "value_per_point",
    }),
    "users": frozenset({
        "created_at", "id", "is_admin", "password_hash", "username",
    }),
}

# The one escape hatch, deliberately EMPTY. A table belongs here only if it can
# never exist on a box that predates its columns — and no table in this schema
# qualifies, because `create_all` builds a new table whole exactly once and from
# then on it is a pre-existing table like any other. It exists so that excluding
# something is a visible, reviewed act with a reason written next to it, rather
# than the silent omission that let #112 and #138 recur.
UNGUARDED_TABLES: frozenset[str] = frozenset()


def test_baseline_tables_exist_in_metadata():
    # Catch a curated table being renamed/dropped out from under this guard.
    for table in BASELINE_COLUMNS:
        assert table in Base.metadata.tables, table


def test_every_table_is_guarded():
    """Fail closed: a table that is neither baselined nor explicitly excused is
    a hole in the guard, and a hole in this guard is how #112 and #138 both
    reached the live box. Adding a model without a baseline should cost thirty
    seconds in CI, not an afternoon of crash-looping services."""
    unguarded = set(Base.metadata.tables) - set(BASELINE_COLUMNS) - UNGUARDED_TABLES
    assert not unguarded, (
        f"table(s) {sorted(unguarded)} are outside the drift guard. Add a "
        f"BASELINE_COLUMNS entry with the table's creation-time column set "
        f"(for a brand-new table that is simply all of its columns), or list "
        f"it in UNGUARDED_TABLES with the reason (CLAUDE.md §6, #112/#138/#193)."
    )
    # An excuse for a table that no longer exists is stale, and stale excuses
    # are how an exclusion outlives the reason for it.
    assert not (UNGUARDED_TABLES - set(Base.metadata.tables))


def test_the_every_fill_tables_are_covered():
    """Named on purpose. `legs` is the table #193 was filed about — an INSERT on
    every fill and a full-model `select(Leg)` on every monitor tick — and
    `position_activities`/`staged_tranches` sit on the same close and staged-entry
    paths. A future refactor that thins BASELINE_COLUMNS should trip here, with
    the reason attached, rather than quietly re-opening the hole."""
    for table in ("legs", "position_activities", "staged_entries",
                  "staged_tranches", "accounts", "execution_strategies"):
        assert table in BASELINE_COLUMNS, table


def test_no_uncovered_column_drift():
    # Every mapped column must be covered either by its frozen creation-time
    # baseline or by an ADD COLUMN migration — on every table, not a curated few.
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
