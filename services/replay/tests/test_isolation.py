"""HARD ISOLATION FROM LIVE TRADING — the acceptance criterion, as a test.

"The harness must be incapable of touching a live order by construction, not by
convention" (#169 §1). A docstring saying so is a convention. This is the
construction:

  * an IMPORT-GRAPH assertion that no order-placing symbol is reachable from the
    replay entrypoint, checked both statically (over the source) and at runtime
    (over `sys.modules` after importing everything the entrypoint imports);
  * a metadata assertion that this service's `create_all` cannot emit DDL for a
    trading table, because its declarative base has never heard of one;
  * an assertion that the harness cannot silently fall back to the trading DSN.

If a future change makes any of these false, CI fails instead of production.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[1]

# Anything that can create, modify or destroy a broker order. Names, not
# modules, so a re-export or an alias is caught too.
FORBIDDEN_SYMBOLS = frozenset({
    "place_order", "cancel_order", "close_position", "modify_position",
    "modify_order", "build_adapter", "PlaceOrderRequest",
    "ModifyPositionRequest", "ClosePositionRequest",
})

# Modules the harness may never import, transitively or otherwise.
FORBIDDEN_MODULES = ("beacon_core.brokers", "beacon_core.bus",
                     "beacon_core.ingest", "beacon_core.notifications", "redis")


def _source_files():
    for p in sorted(SERVICE_ROOT.rglob("*.py")):
        if "tests" in p.parts:
            continue
        yield p


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_no_source_file_imports_an_order_placing_module():
    offenders = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for mod in _imports(tree):
            if any(mod == f or mod.startswith(f + ".") for f in FORBIDDEN_MODULES):
                offenders.append(f"{path.name}: {mod}")
    assert not offenders, f"replay must not import: {offenders}"


def test_no_source_file_references_an_order_placing_symbol():
    """Catches the alias route too: `from x import place_order as go`."""
    offenders = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    if a.name in FORBIDDEN_SYMBOLS:
                        offenders.append(f"{path.name}: import {a.name}")
                continue
            if name in FORBIDDEN_SYMBOLS:
                offenders.append(f"{path.name}: {name}")
    assert not offenders, f"order-placing symbols reachable: {offenders}"


def test_the_runtime_import_graph_of_the_entrypoint_holds_no_broker_module():
    """Static analysis misses a transitive import, so this checks the real thing:
    a FRESH interpreter that imports the container's entrypoint and reports what
    landed in `sys.modules`.

    A subprocess rather than an in-process import, because this suite runs
    alongside `packages/core/tests` — which legitimately imports the broker
    adapters — and an in-process check would either skip (proving nothing) or
    fail for someone else's import."""
    probe = (
        "import sys, json;"
        f"sys.path.insert(0, {str(SERVICE_ROOT)!r});"
        "import main, harness.runner, harness.store;"
        "print(json.dumps(sorted(sys.modules)))"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert out.returncode == 0, out.stderr
    loaded = json.loads(out.stdout.strip().splitlines()[-1])
    leaked = sorted(m for m in loaded
                    if any(m == f or m.startswith(f + ".")
                           for f in FORBIDDEN_MODULES))
    assert not leaked, f"forbidden modules loaded by the entrypoint: {leaked}"


def test_create_all_cannot_emit_ddl_for_a_trading_table():
    from harness.models import QUALIFIED_TABLES, ReplayBase
    assert set(ReplayBase.metadata.tables) == set(QUALIFIED_TABLES)
    for forbidden in ("trades", "legs", "signals", "position_activities",
                      "execution_strategies", "settings", "candles"):
        assert forbidden not in ReplayBase.metadata.tables
        assert f"public.{forbidden}" not in ReplayBase.metadata.tables


def test_every_replay_table_lives_in_the_replay_schema():
    """The harness owns `replay` and has NO create privilege in `public`. A model
    that forgot its `__table_args__` schema would try to create a table next to
    the trading ones — which is both an isolation hole and a hard failure at
    deploy, since the role cannot create there."""
    from harness.models import REPLAY_SCHEMA, ReplayBase
    for name, table in ReplayBase.metadata.tables.items():
        assert table.schema == REPLAY_SCHEMA, f"{name} is not in {REPLAY_SCHEMA}"


def test_the_replay_schema_is_not_named_after_the_role():
    """The default `search_path` is `"$user", public`. A schema named
    `beacon_replay` would shadow `public` for that role, so an unqualified read
    of `signals` could resolve somewhere other than the trading table."""
    from harness.models import REPLAY_SCHEMA
    assert REPLAY_SCHEMA == "replay"


def test_the_role_sql_grants_no_write_on_the_trading_schema():
    """The grant script is the isolation. Reviewing it by eye is how the first
    version shipped a GRANT that could not run, so the invariants are asserted."""
    sql = (SERVICE_ROOT / "sql" / "replay_role.sql").read_text(encoding="utf-8")
    body = sql.split("=== VERIFY", 1)[0]
    statements = [s.strip() for s in body.split(";")
                  if s.strip() and not s.strip().startswith("--")]
    for stmt in statements:
        head = "\n".join(l for l in stmt.splitlines()
                         if not l.strip().startswith("--")).upper()
        if head.startswith("GRANT") and " ON SCHEMA PUBLIC" in head:
            assert "CREATE" not in head.split(" ON SCHEMA PUBLIC")[0], \
                "the replay role must never get CREATE on public"
        if head.startswith("GRANT") and "IN SCHEMA PUBLIC" in head:
            for verb in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert verb not in head.split("ON ALL TABLES")[0], \
                    f"the replay role must never get {verb} on public"


def test_the_role_sql_has_no_grant_on_an_object_that_cannot_exist_yet():
    """The first cut of this script ended with `GRANT … ON replay_runs,
    replay_results` and a grant on their sequences — statements that CANNOT run,
    because nothing has created those objects at that point and the role had no
    privilege to create them. Owning a schema removed the need entirely; this
    keeps it removed."""
    sql = (SERVICE_ROOT / "sql" / "replay_role.sql").read_text(encoding="utf-8")
    body = sql.split("=== VERIFY", 1)[0].upper()
    assert "ON REPLAY_RUNS" not in body
    assert "ON SEQUENCE" not in body
    assert "CREATE SCHEMA IF NOT EXISTS REPLAY" in body
    assert "GRANT USAGE, CREATE ON SCHEMA REPLAY" in body


def test_the_role_sql_needs_no_superuser():
    """`CREATE SCHEMA … AUTHORIZATION <role>` makes that role the schema owner,
    and creating a schema owned by someone else requires being able to SET ROLE
    to them. A non-superuser cannot — which is every managed Postgres — so that
    form failed with `must be able to SET ROLE "beacon_replay"` (SQLSTATE 42501).

    Nothing in the script may depend on SET ROLE, and the two constructs that
    would are named here rather than left to a reviewer to notice."""
    sql = (SERVICE_ROOT / "sql" / "replay_role.sql").read_text(encoding="utf-8")
    body = sql.split("=== VERIFY", 1)[0]
    executable = "\n".join(l for l in body.splitlines()
                           if not l.strip().startswith("--")).upper()
    assert "AUTHORIZATION" not in executable
    assert "SET ROLE" not in executable


def test_the_role_sql_scopes_default_privileges_to_the_table_owner():
    """`ALTER DEFAULT PRIVILEGES` without `FOR ROLE` follows whoever RAN it, not
    whoever creates the tables. Omitting it scopes the rule to the superuser
    while `create_all` runs as the app role — so the next table added to
    models.py would be invisible to the harness for no discoverable reason."""
    sql = (SERVICE_ROOT / "sql" / "replay_role.sql").read_text(encoding="utf-8")
    body = sql.split("=== VERIFY", 1)[0].upper()
    assert "ALTER DEFAULT PRIVILEGES FOR ROLE" in body


def test_the_replay_base_is_not_the_trading_base():
    from beacon_core.db.base import Base as TradingBase
    from harness.models import ReplayBase
    assert ReplayBase is not TradingBase
    assert ReplayBase.metadata is not TradingBase.metadata


def test_the_harness_refuses_to_start_without_its_own_dsn(monkeypatch):
    """Falling back to DATABASE_URL would quietly re-couple the harness to the
    trading role. A missing value must be an error, not a default."""
    from harness import store
    monkeypatch.delenv(store.DSN_ENV, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://trading@host/db")
    with pytest.raises(RuntimeError) as exc:
        store.dsn()
    assert store.DSN_ENV in str(exc.value)
    assert "DATABASE_URL" in str(exc.value)       # says why, not just what


def test_the_harness_uses_the_dsn_it_is_given(monkeypatch):
    from harness import store
    monkeypatch.setenv(store.DSN_ENV, "postgresql+asyncpg://replay@host/db")
    assert store.dsn().startswith("postgresql+asyncpg://replay@")


def test_the_compose_entry_holds_no_broker_credentials():
    """`env_file: .env` would hand this container the Capital.com variables. The
    compose entry must name its environment explicitly instead."""
    compose = (SERVICE_ROOT.parents[1] / "docker-compose.yml").read_text(
        encoding="utf-8")
    block = compose.split("replay:", 1)[1].split("\n  frontend:", 1)[0]
    assert "env_file" not in block
    assert "REPLAY_DATABASE_URL" in block
    # the TRADING dsn, as its own key — not the REPLAY_ prefixed one
    assert "\n      DATABASE_URL" not in block
    assert "CAPITAL" not in block.upper()
    assert "profiles:" in block and "research" in block


def test_the_service_declares_the_tables_it_may_write():
    from harness.models import REPLAY_TABLES
    assert REPLAY_TABLES == ("replay_runs", "replay_results")


def test_the_one_way_rule_holds_in_the_other_direction_too():
    """Research may read prod; prod must NEVER import research (#60 ADR). A
    trading service that grew a `from harness...` import would couple the executor's
    hot path to a research module — checked from this side because this is the
    side that knows what the research package is called."""
    repo = SERVICE_ROOT.parents[1]
    roots = [repo / "packages" / "core" / "beacon_core"]
    roots += [repo / "services" / s
              for s in ("api", "executor", "monitor", "telegram")]
    offenders = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            # Matched on the name `replay`, not on a bare `app.` — `services/api`
            # has its own `app` package and would false-positive on that.
            for mod in _imports(tree):
                if "replay" in mod.split("."):
                    offenders.append(f"{path.relative_to(repo)}: {mod}")
    assert not offenders, f"prod imports research: {offenders}"
