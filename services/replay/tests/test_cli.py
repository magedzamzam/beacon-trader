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
                                "coverage", "check", "worker"}


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


def test_check_resolves_a_generator_config_without_a_database(capsys):
    """The generated-signal half of the same guarantee (#184). An indicator the
    condition names but the registry does not carry is silently UNKNOWN on every
    bar — the generator emits nothing and nothing errors. `check` is where that
    becomes visible, so it has to resolve the instance list offline."""
    import main
    rc = main.main(["check", "--config",
                    str(SERVICE_ROOT / "runs" / "example-generator-rules.json")])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["signal_source"] == "generator:rules"
    gen = out["generator"]
    assert gen["name"] == "rules" and gen["timeframe"] == "15m"
    assert {i.split(":", 1)[1].split("_")[0] for i in gen["indicator_instances"]} \
        >= {"macd", "rsi", "fvg"}
    # The caps are not optional — an example that left them off would teach the
    # wrong thing (see harness/generators.py).
    assert gen["cooldown_bars"] > 0 and gen["max_signals_per_day"] > 0


def test_a_broken_generator_config_fails_the_check_instead_of_the_sweep(capsys, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({
        "signal_source": "generator:rules", "variants": [],
        "generator_config": {"timeframe": "15m",
                             "long": {"when": {"type": "always"}}},
    }), encoding="utf-8")
    import main
    assert main.main(["check", "--config", str(bad)]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_the_generator_example_restates_that_it_is_not_a_route_to_live():
    """A backtest is the SCREENING step. The Lever-5 chain has to be stated in
    the config an operator actually opens, not only in a docstring."""
    text = (SERVICE_ROOT / "runs" / "example-generator-rules.json").read_text(
        encoding="utf-8")
    assert "kind='engine'" in text
    assert "shadow forward-R" in text
    assert "validation gate" in text
    cfg = json.loads(text)
    assert cfg["holdout_from"]              # in-sample-only would not be an edge


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


# --- the portal's scaffolded launch (#183) ------------------------------------
def test_the_portal_ladders_are_named_not_authored_by_the_browser():
    """The launch form sends ladder NAMES. It sent `{name}` and nothing else
    once, and the sweep evaluated 1,873 signals, took ZERO, and still reported
    `done` with 5,619 rows — every account lookup missed because the browser had
    invented a variant with no accounts, risk or instrument.

    Naming the ladders server-side makes that unrepresentable."""
    import main
    assert set(main.PORTAL_LADDERS) == {"be_at_tp1", "be_at_tp2", "runner_no_ratchet"}
    for name, rules in main.PORTAL_LADDERS.items():
        assert rules and all("trigger" in r and "action" in r for r in rules), name


def test_the_control_arm_is_a_rule_that_can_never_fire():
    """An EMPTY sl_rules list reads as UNSET and cascades to the default ladder,
    so a control expressed as `[]` would silently be BE@TP1."""
    import main
    runner = main.PORTAL_LADDERS["runner_no_ratchet"]
    assert runner[0]["trigger"]["index"] == 99


def test_the_page_sends_names_and_never_sl_rules():
    """If the page ever starts authoring exit rules again, this fails.

    The form now sends an exit by NAME ("be_at_tp1") and the worker resolves it
    through `harness.whatif.EXITS`, for the same reason the scaffold resolves
    ladders server-side: a browser that authors execution config can express a
    variant no live account would ever run, and the run still reports `done`."""
    page = (SERVICE_ROOT.parents[1] / "frontend/src/pages/Replay.jsx").read_text(
        encoding="utf-8")
    assert '"whatif"' in page
    assert "sl_rules" not in page, "the browser must not author exit rules"
    assert "move_sl_to" not in page
    assert "entry_filters" not in page, "nor filtration rules"


def test_every_exit_the_page_offers_is_one_the_worker_knows():
    """A dropdown option with no server-side resolver behind it is a silent
    no-op: the arm runs with the default exit and the report says the change
    made no difference, which is a wrong answer rather than an error."""
    import re

    from harness import whatif as W
    page = (SERVICE_ROOT.parents[1] / "frontend/src/pages/Replay.jsx").read_text(
        encoding="utf-8")
    trig = set(re.findall(r'v:\s*"([^"]+)"',
                          page.split("const TRIGGERS = [", 1)[1].split("];", 1)[0]))
    act = set(re.findall(r'v:\s*"([^"]+)"',
                         page.split("const ACTIONS = [", 1)[1].split("];", 1)[0]))
    assert trig and act
    for k in trig:
        assert W.trigger_of({"kind": k, "index": 1, "points": 1, "r": 1}), k
    for k in act:
        assert W.action_of({"kind": k, "index": 1}), k
    # The whole-ladder modes the page offers must resolve too.
    modes = set(re.findall(r'v:\s*"([^"]*)"',
                           page.split("const EXIT_MODES = [", 1)[1].split("];", 1)[0]))
    for m in modes - {"", "custom"}:
        assert m in W.EXITS, m


def test_the_page_never_offers_previous_target_on_a_non_tp_trigger():
    """The engine resolves `previous_tp` from the TRIGGER's index, so pairing it
    with a price or R trigger yields a rule with no target that silently does
    nothing. Refused on both sides."""
    from harness import whatif as W
    assert W.step_rule({"when": {"kind": "points", "points": 30},
                        "then": {"kind": "previous_tp"}}) is None
    assert W.step_rule({"when": {"kind": "tp", "index": 2},
                        "then": {"kind": "previous_tp"}}) is not None
    page = (SERVICE_ROOT.parents[1] / "frontend/src/pages/Replay.jsx").read_text(
        encoding="utf-8")
    assert 'ACTIONS.filter(x => x.v !== "previous_tp")' in page


def test_every_quick_pick_the_page_offers_resolves_to_a_condition():
    """The quick picks are shortcuts into the SAME builder, so each one has to
    be a condition the worker can turn into an engine leaf."""
    import json
    import re

    from harness import whatif as W
    page = (SERVICE_ROOT.parents[1] / "frontend/src/pages/Replay.jsx").read_text(
        encoding="utf-8")
    block = page.split("const QUICK = [", 1)[1].split("];", 1)[0]
    conds = re.findall(r"cond:\s*(\{.*?\})\s*\}", block)
    assert conds, "the page should offer quick picks"
    for raw in conds:
        c = json.loads(re.sub(r'(\w+):', r'"\1":', raw))
        assert W.keep_leaf(c) is not None, c


def test_the_builders_vocabulary_matches_the_one_the_api_accepts():
    """Three lists have to agree or a perfectly reasonable request 400s: what
    the page offers, what the API validates, and what the worker resolves."""
    import re

    from harness import whatif as W
    api = (SERVICE_ROOT.parents[1] / "services/api/app/routers/replay.py").read_text(
        encoding="utf-8")
    page = (SERVICE_ROOT.parents[1] / "frontend/src/pages/Replay.jsx").read_text(
        encoding="utf-8")

    def named(src, const):
        return set(re.findall(r'"([^"]+)"',
                              src.split(const, 1)[1].split(")", 1)[0]))
    api_trig = named(api, "TRIGGER_KINDS = (")
    api_act = named(api, "ACTION_KINDS = (")
    page_trig = set(re.findall(r'v:\s*"([^"]+)"',
                               page.split("const TRIGGERS = [", 1)[1].split("];", 1)[0]))
    page_act = set(re.findall(r'v:\s*"([^"]+)"',
                              page.split("const ACTIONS = [", 1)[1].split("];", 1)[0]))
    assert page_trig == api_trig, (page_trig, api_trig)
    assert page_act == api_act, (page_act, api_act)
    for k in api_trig:
        assert W.trigger_of({"kind": k, "index": 1, "points": 1, "r": 1})
    for k in api_act:
        assert W.action_of({"kind": k, "index": 1})


def test_the_trade_list_asks_for_arms_the_worker_actually_writes():
    """The drill-down filters `replay_results` by variant name. The worker names
    the two arms when it stores them, and a page asking for "control" or "alt"
    would get an empty list and read as "no rows stored for this run" — a wrong
    answer that looks like missing data rather than a typo."""
    import re

    main_src = (SERVICE_ROOT / "main.py").read_text(encoding="utf-8")
    written = set(re.findall(r'"(baseline|whatif)":\s*\w+_res', main_src))
    assert written == {"baseline", "whatif"}, written

    page = (SERVICE_ROOT.parents[1] / "frontend/src/pages/Replay.jsx").read_text(
        encoding="utf-8")
    block = page.split("function Trades(", 1)[1]
    asked = set(re.findall(r'\["(baseline|whatif)",', block))
    assert asked == written, (asked, written)


def test_the_trade_list_never_shows_leg_level_money():
    """Trade-level P&L is trustworthy; leg-level is not (CLAUDE.md 2.5). The
    per-leg line shows prices, size and an outcome LABEL — putting money on it
    would invite exactly the attribution the repo has ruled out."""
    page = (SERVICE_ROOT.parents[1] / "frontend/src/pages/Replay.jsx").read_text(
        encoding="utf-8")
    block = page.split("function Trades(", 1)[1]
    leg_block = block.split("row.legs.map(", 1)[1].split("</div>", 1)[0]
    assert "l.realized_pl" not in leg_block
    assert "l.lot" in leg_block and "l.fill_price" in leg_block
