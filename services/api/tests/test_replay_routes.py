"""The replay router's surface, its read-only-ness, and the one-way rule (#183).

Three things are asserted here, and each one exists because the alternative is
discovered in production:

  * the router serves EXACTLY three GETs and nothing that writes or starts a
    run — a browser-startable 400k-bar sweep would compete with `monitor` for
    CPU on the box that manages open positions;
  * `services/api` does not import `harness.*` — the mirror of
    `services/replay/tests/test_isolation.py::test_the_one_way_rule_holds_in_the_other_direction_too`,
    checked from the PROD side so a future `from harness.models import ...`
    here fails CI rather than coupling every python image to research code;
  * every `/replay/...` path the client builds is served — this box has no npm,
    so a test is the only thing between a client/router drift and the operator
    (same contract as #175/#182).

Pure: imports the router object and reads api.js as text — no TestClient, no DB.
"""
import ast
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routers import replay as R

REPO_ROOT = Path(__file__).resolve().parents[3]
API_JS = REPO_ROOT / "frontend/src/lib/api.js"
API_APP = REPO_ROOT / "services/api/app"
REPLAY_PAGE = REPO_ROOT / "frontend/src/pages/Replay.jsx"

EXPECTED = {
    ("GET", "/replay/runs"),
    ("GET", "/replay/runs/{run_id}"),
    ("GET", "/replay/runs/{run_id}/results"),
    # The portal drives backtesting end to end now. These ENQUEUE; a separate
    # worker executes. See `test_the_api_enqueues_and_never_executes`.
    ("POST", "/replay/jobs"),
    ("GET", "/replay/jobs"),
    ("POST", "/replay/jobs/{job_id}/cancel"),
}


def _served() -> set:
    out = set()
    for r in R.router.routes:
        for method in (r.methods or set()):
            if method not in ("HEAD", "OPTIONS"):
                out.add((method, r.path))
    return out


def test_router_serves_exactly_the_expected_surface():
    assert _served() == EXPECTED


def test_the_api_enqueues_and_never_executes():
    """The guarantee, restated for the queue.

    The original rule was "no write route at all", because a browser-startable
    400k-bar sweep is a research job that can starve the trading path. The
    operator asked for portal-driven backtesting, so the rule is now enforced by
    architecture: the API may APPEND to a queue and nothing else. It must never
    import the harness, spawn a process, or run a sweep in-process — a separate
    nice'd worker does that, one job at a time.

    Anything here that could execute would put a sweep in the API's event loop,
    which is the failure this whole design exists to prevent."""
    src = Path(R.__file__).read_text(encoding="utf-8")
    body = " ".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    for forbidden in ("subprocess", "multiprocessing", "ProcessPool", "ThreadPool",
                      "os.system", "os.fork", "os.exec", "popen",
                      "PortfolioSim", "R.sweep", "run_variant", "asyncio.create_task"):
        assert forbidden not in body, (
            f"the replay router must never {forbidden} — it enqueues, the worker runs")


def test_a_whatif_is_validated_on_its_question_not_on_a_variants_list():
    """A what-if names a scope, a window and a change; the worker builds both
    arms from the live config. Requiring a `variants` list here would force the
    browser to send a stub one, and a browser that authors arms is exactly how
    the previous launch button produced runs that took zero trades.

    Each rejection below is a request that would otherwise reach the queue, run
    for minutes, and produce a report that is empty or confidently wrong."""
    ok = {"mode": "whatif", "scope": {"type": "source", "source_id": 3},
          "changes": {"exit": "be_at_tp1"}}
    R._check_whatif(ok)                                   # no variants: fine

    bad = [
        ({"mode": "whatif", "changes": {"exit": "be_at_tp1"}}, "scope"),
        ({"mode": "whatif", "scope": {"type": "everything"},
          "changes": {"exit": "be_at_tp1"}}, "scope"),
        # A named scope with nothing named in it silently widens to the whole
        # book, which is a different question than the one that was asked.
        ({"mode": "whatif", "scope": {"type": "source"},
          "changes": {"exit": "be_at_tp1"}}, "source_id"),
        ({"mode": "whatif", "scope": {"type": "account"},
          "changes": {"exit": "be_at_tp1"}}, "account_id"),
        # The dangerous one: both arms identical, so the report says "no
        # difference" — a wrong answer rather than an error.
        ({"mode": "whatif", "scope": {"type": "manual"}, "changes": {}}, "change"),
        ({"mode": "whatif", "scope": {"type": "manual"}}, "changes"),
    ]
    for cfg, needle in bad:
        with pytest.raises(HTTPException) as exc:
            R._check_whatif(cfg)
        assert exc.value.status_code == 400
        assert needle in str(exc.value.detail)


def test_a_free_form_entry_condition_is_accepted():
    """The operator asked not to be limited to a fixed menu, so ANY registry
    indicator is allowed — 45 of them, FVG and order blocks included. The bound
    is on how many conditions, never on which."""
    for cond in ({"kind": "indicator", "id": "fvg", "field": "present",
                  "op": "is_true", "timeframe": "15m"},
                 {"kind": "indicator", "id": "order_block", "field": "dist_pct",
                  "op": "lte", "value": 0.5},
                 {"kind": "session", "sessions": ["London"]},
                 {"kind": "not_session", "sessions": ["New York"]},
                 {"kind": "regime", "trending": False}):
        R._check_whatif({"mode": "whatif", "scope": {"type": "manual"},
                         "changes": {"conditions": [cond]}})


def test_a_condition_the_worker_cannot_resolve_is_refused():
    """An unknown kind, or an indicator with no operator, resolves to nothing —
    the arm would run unfiltered and the report would call it a filtered run."""
    for bad, needle in (({"kind": "vibes"}, "condition.kind"),
                        ({"kind": "indicator", "id": "rsi"}, "op"),
                        ({"kind": "indicator", "op": "lt"}, "id")):
        with pytest.raises(HTTPException) as exc:
            R._check_whatif({"mode": "whatif", "scope": {"type": "manual"},
                             "changes": {"conditions": [bad]}})
        assert exc.value.status_code == 400 and needle in str(exc.value.detail)


def test_a_free_form_exit_ladder_is_accepted():
    R._check_whatif({"mode": "whatif", "scope": {"type": "manual"},
                     "changes": {"exit_steps": [
                         {"when": {"kind": "points", "points": 30},
                          "then": {"kind": "breakeven"}},
                         {"when": {"kind": "tp", "index": 2},
                          "then": {"kind": "previous_tp"}},
                         {"when": {"kind": "r", "r": 1.5},
                          "then": {"kind": "tp", "index": 1}}]}})


def test_previous_target_is_refused_on_a_trigger_that_has_no_target():
    """The engine reads `previous_tp` off the TP that fired, so on a price or R
    trigger the step resolves to no target and silently does nothing. Caught at
    the door rather than shipped as a run that reports "no difference"."""
    with pytest.raises(HTTPException) as exc:
        R._check_whatif({"mode": "whatif", "scope": {"type": "manual"},
                         "changes": {"exit_steps": [
                             {"when": {"kind": "r", "r": 1.0},
                              "then": {"kind": "previous_tp"}}]}})
    assert exc.value.status_code == 400
    assert "previous target" in str(exc.value.detail)


def test_the_builder_is_bounded_so_one_request_cannot_own_the_worker():
    """A run is minutes of the worker's ONLY thread."""
    many = [{"kind": "regime", "trending": True}] * (R.MAX_CONDITIONS + 1)
    with pytest.raises(HTTPException):
        R._check_whatif({"mode": "whatif", "scope": {"type": "manual"},
                         "changes": {"conditions": many}})
    steps = [{"when": {"kind": "tp", "index": 1}, "then": {"kind": "breakeven"}}] \
        * (R.MAX_EXIT_STEPS + 1)
    with pytest.raises(HTTPException):
        R._check_whatif({"mode": "whatif", "scope": {"type": "manual"},
                         "changes": {"exit_steps": steps}})


def test_a_whatif_scope_of_manual_needs_no_id():
    """"Everything" is a real scope — the whole book is the baseline."""
    R._check_whatif({"mode": "whatif", "scope": {"type": "manual"},
                     "changes": {"filters": [{"kind": "only_trending"}]}})


def test_the_only_writes_are_to_the_job_queue():
    """Results stay immutable to the platform. A result the platform can edit is
    not a record of what the simulator produced; a queue it can append to is
    just a queue."""
    src = Path(R.__file__).read_text(encoding="utf-8")
    body = " ".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    for table in ("RUNS", "RESULTS"):
        for verb in (".insert()", ".update()", ".delete()"):
            assert f"{table}{verb}" not in body, f"{table}{verb} must never appear"
    assert "JOBS.insert()" in body and "JOBS.update()" in body


def test_the_write_surface_is_only_the_queue():
    writes = {(m, p) for m, p in _served() if m != "GET"}
    assert writes == {("POST", "/replay/jobs"),
                      ("POST", "/replay/jobs/{job_id}/cancel")}


def test_the_read_model_is_not_attached_to_the_trading_metadata():
    """The API runs `create_all` on the trading metadata at startup. A replay
    table attached to it would make the API try to CREATE in a schema it holds
    SELECT on — a crash loop AND an isolation hole."""
    from beacon_core.db.base import Base

    assert R._md is not Base.metadata
    assert set(R._md.tables) == {"replay.replay_runs", "replay.replay_results",
                                 "replay.replay_jobs"}
    for name in Base.metadata.tables:
        assert not name.startswith("replay.")


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_services_api_does_not_import_the_replay_harness():
    """#60's one-way rule: research may read prod; prod must NEVER import
    research. Reading the `replay.*` TABLES over SQL is a read, not an import —
    `from harness import ...` is the thing that is forbidden."""
    offenders = []
    for path in sorted(API_APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for mod in _imports(tree):
            head = mod.split(".")[0]
            if head in ("harness", "replay") or "harness" in mod.split("."):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {mod}")
    assert not offenders, f"prod imports research: {offenders}"


def test_every_replay_path_the_client_builds_is_served():
    js = API_JS.read_text(encoding="utf-8")
    paths = set(re.findall(r"[\"`](/replay/[^\"`?]*)", js))
    assert paths, "the client should call the replay API"
    # Both sides collapse to `{}` — hardcoding one param name meant a route with
    # a differently-named param (job_id) read as unserved.
    served = {re.sub(r"\{[^}]+\}", "{}", p).rstrip("/") for _m, p in _served()}
    for p in paths:
        norm = re.sub(r"\$\{[^}]+\}", "{}", p).rstrip("/")
        assert norm in served, (p, norm, sorted(served))


def test_the_client_can_queue_but_not_delete():
    """The portal may request and cancel. It may never DELETE a run or a result —
    those are the record of what the simulator produced."""
    js = API_JS.read_text(encoding="utf-8")
    block = js.split("replayEnqueue:", 1)[1].split("// brokers", 1)[0]
    assert "post(" in block, "the portal must be able to queue a run"
    assert "del(" not in block, "the replay client must never DELETE"


def test_the_cli_front_door_is_still_named_by_the_api():
    """The page no longer quotes it, and should not: Backtest queues its own runs
    now, so an empty state telling the operator to SSH would be a lie about the
    product. But the harness is still driven from a shell for sweeps, and the API
    response is the only place that name survives � so it is asserted here."""
    assert "main.py run --config" in R.RUN_COMMAND


def test_the_quant_guardrails_ride_on_the_payload_not_the_page():
    """#169 �8 said every guardrail must be visible without interaction, and the
    page that rendered them is gone � Backtest is a screening tool now, and the
    ranking it used to show moved to the API and the harness.

    So the requirement moves with it. A headline that travels without its basis
    and its search count is the failure being prevented, and it does not matter
    whether the reader is a browser or a script."""
    out = R._headline_of(_summary(basis="in_sample", searched=12, withheld=True))
    for field in ("headline_basis", "n_variants_searched",
                  "best_of_n_inflation_sigma", "all_verdicts_withheld",
                  "n_verdicts_withheld", "n_ranked"):
        assert field in out, f"the run payload must carry {field}"
    src = (REPO_ROOT / "services/api/app/routers/replay.py").read_text(encoding="utf-8")
    assert "**_headline_of(" in src, "every listed run must carry its guardrails"


def test_the_page_can_launch_a_run():
    """The operator asked for backtesting to be fully front-end driven: no SSH,
    no CLI. The page must actually be able to queue one."""
    page = REPLAY_PAGE.read_text(encoding="utf-8")
    assert "replayEnqueue" in page
    assert "replayJobs" in page


def test_the_page_asks_one_question_and_states_the_two_answers():
    """The rebuild's whole point. The screen exists to answer "would we have made
    money doing it differently", so it must ask for a scope, a window and a
    change, and it must show BOTH arms � a what-if number with nothing to
    compare it against is the thing that made the last page useless."""
    page = REPLAY_PAGE.read_text(encoding="utf-8")
    assert '"whatif"' in page and "changes:" in page
    assert "baseline" in page and "verdict" in page
    # The jargon the operator asked to be rid of. If any of it comes back onto
    # this page, it has stopped being the screening tool they asked for.
    # Comments are stripped first: the header names these terms in order to say
    # where they went and why, and that explanation is the opposite of the drift
    # being guarded against.
    code = re.sub(r"/\*.*?\*/", "", page, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    for jargon in ("best_of_n_inflation_sigma", "credible", "headline_basis",
                   "holdout", "delever", "bootstrap"):
        assert jargon not in code, f"{jargon} does not belong on the what-if page"


# --- the guardrail fold-up, unit-tested ---------------------------------------
def _summary(*, basis, searched, withheld):
    return {
        "variants": {"v1": {"headline_basis": basis,
                            "guardrails": {"n_variants_searched": searched,
                                           "best_of_n_inflation_sigma": 2.4}}},
        "ranking": [{"variant": "v1", "verdict_withheld": withheld}],
    }


def test_a_single_in_sample_arm_makes_the_whole_run_read_in_sample():
    """`in_sample` must win over `held_out` when a run mixes them. The failure
    this prevents is a run picker that labels a run held-out because one arm
    was, while the number the operator opens is in-sample."""
    s = _summary(basis="held_out", searched=4, withheld=False)
    s["variants"]["v2"] = {"headline_basis": "in_sample", "guardrails": {}}
    assert R._headline_of(s)["headline_basis"] == "in_sample"


def test_the_search_count_is_the_whole_grid_not_the_arms_shown():
    s = _summary(basis="held_out", searched=60, withheld=False)
    out = R._headline_of(s)
    assert out["n_variants_searched"] == 60
    assert out["best_of_n_inflation_sigma"] == 2.4


def test_all_verdicts_withheld_is_false_when_there_is_nothing_ranked():
    assert R._headline_of({})["all_verdicts_withheld"] is False
    s = _summary(basis="held_out", searched=2, withheld=True)
    out = R._headline_of(s)
    assert out["all_verdicts_withheld"] is True
    assert out["n_verdicts_withheld"] == 1


class _Run:
    def __init__(self, validation, *, id=1, git_sha="abc123", candle_digest="cd1"):
        self.validation = validation
        self.id = id
        self.git_sha = git_sha
        self.candle_digest = candle_digest


class _Hit:
    """A row from `_validation_index` — id + the stored verdict."""
    def __init__(self, id, validation):
        self.id = id
        self.validation = validation


PASSED = {"gate": {"passed": True, "failures": []}}


def test_a_sweep_inherits_the_gate_that_covers_its_code_and_its_bars():
    """The gate validates the SIMULATOR — a code version over a set of bars — not
    one sweep. Every stored sweep used to show an amber "not validated" banner
    while a passing gate sat in the same table, which was simply false."""
    index = {("abc123", "cd1"): _Hit(9, PASSED)}
    got = R._validation_of(_Run(None, id=12), index)
    assert got["ran"] is True and got["passed"] is True
    assert got["source"] == "inherited" and got["from_run_id"] == 9


def test_an_inherited_verdict_is_never_reported_as_the_runs_own():
    """Covered-by-a-gate and reconciled-against-broker-truth are different
    claims, and the UI renders them differently."""
    index = {("abc123", "cd1"): _Hit(9, PASSED)}
    assert R._validation_of(_Run(None, id=12), index)["source"] == "inherited"
    assert R._validation_of(_Run(PASSED, id=9), index)["source"] == "own"


def test_a_runs_own_verdict_beats_anything_inheritable():
    failed = {"gate": {"passed": False, "failures": ["median |delta R| too high"]}}
    got = R._validation_of(_Run(failed, id=12), {("abc123", "cd1"): _Hit(9, PASSED)})
    assert got["passed"] is False and got["source"] == "own"


def test_different_code_or_different_bars_inherits_nothing():
    """A gate on other code, or over other candles, is a gate on a different
    simulator. Inheriting across either would launder an unvalidated run."""
    index = {("abc123", "cd1"): _Hit(9, PASSED)}
    assert R._validation_of(_Run(None, git_sha="other"), index)["ran"] is False
    assert R._validation_of(_Run(None, candle_digest="other"), index)["ran"] is False
    assert R._validation_of(_Run(None, git_sha=None), index)["ran"] is False
    assert R._validation_of(_Run(None, candle_digest=None), index)["ran"] is False


def test_with_no_index_a_run_still_reports_honestly():
    assert R._validation_of(_Run(None))["ran"] is False
    assert R._validation_of(_Run(None))["source"] is None


def test_a_gate_that_never_ran_is_not_a_gate_that_passed():
    """`ran: false` and `passed: false` have different fixes and the UI has to
    be able to say which — but both mean the counterfactual is not actionable."""
    assert R._validation_of(_Run(None)) == {
        "ran": False, "source": None, "from_run_id": None,
        "passed": None, "failures": [], "systematic_bias": None}
    failed = R._validation_of(_Run({"gate": {"passed": False,
                                             "failures": ["median |delta R| 0.4 > 0.25"],
                                             "systematic_bias": "optimistic"}}))
    assert failed["ran"] is True and failed["passed"] is False
    assert failed["systematic_bias"] == "optimistic"
