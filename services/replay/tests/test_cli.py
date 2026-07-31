"""The CLI surface.

`check` is exercised for real (it is the one command that touches no database).
The rest are asserted structurally — that the subcommands exist, and that `run`
creates its tables BEFORE it simulates anything. The ordering is the point: a
missing grant that surfaces after 800 signals x N variants have been simulated
costs an afternoon and teaches nothing, which is exactly what shipped.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def test_every_subcommand_is_wired_to_a_handler():
    import main
    p = main.build_parser()
    sub = next(a for a in p._actions if a.choices and "run" in a.choices)
    assert set(sub.choices) == {"init", "scaffold", "run", "validate",
                                "coverage", "check"}


def test_check_validates_the_example_config_without_a_database(capsys):
    """`check` must not need the DB — it is what catches a malformed variant
    before a sweep burns an afternoon on it."""
    import main
    rc = main.main(["check", "--config",
                    str(SERVICE_ROOT / "runs" / "example-exit-ladder.json")])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["n_variants"] == 5
    assert {v["name"] for v in out["variants"]} == {
        "be_at_tp1", "be_at_tp2", "be_lock_0_6r", "per_channel_mixed",
        "staged_entry"}
    assert len({v["digest"] for v in out["variants"]}) == 5


def test_the_example_config_expresses_a_per_account_source_variant():
    """§6 requires per-(account, source) variants to be a first-class thing, so
    the shipped example has to demonstrate one or it is not documentation."""
    cfg = json.loads((SERVICE_ROOT / "runs" / "example-exit-ladder.json")
                     .read_text(encoding="utf-8"))
    mixed = next(v for v in cfg["variants"] if v["name"] == "per_channel_mixed")
    scopes = {(s.get("account_id"), s.get("source_id")) for s in mixed["strategies"]}
    assert (None, None) in scopes            # the base layer
    assert any(a is not None and s is not None for a, s in scopes)


def _run_body():
    tree = ast.parse((SERVICE_ROOT / "main.py").read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "cmd_run")


def _calls_in_order(fn):
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in ("init_replay_tables", "sweep"):
                out.append((node.lineno, name))
    return [n for _, n in sorted(out)]


def test_run_creates_its_tables_before_it_simulates_anything():
    order = _calls_in_order(_run_body())
    assert order, "cmd_run should call both init_replay_tables and sweep"
    assert order.index("init_replay_tables") < order.index("sweep"), (
        "a missing grant must fail in seconds, not after a completed sweep")


def test_a_dry_run_writes_nothing():
    """`--dry-run` prints the report and touches no table — so it stays usable
    on a box where the write grant is not set up at all."""
    src = ast.unparse(_run_body())
    head = src.split("out = R.sweep", 1)[0]
    assert "if not args.dry_run" in head, (
        "init_replay_tables must be guarded by --dry-run")


def test_validate_exits_non_zero_on_a_failed_gate():
    """A validation step that always exits 0 is decoration."""
    src = ast.unparse(next(
        n for n in ast.walk(ast.parse(
            (SERVICE_ROOT / "main.py").read_text(encoding="utf-8")))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "cmd_validate"))
    assert "return 1 if failed else 0" in src
