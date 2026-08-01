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

from app.routers import replay as R

REPO_ROOT = Path(__file__).resolve().parents[3]
API_JS = REPO_ROOT / "frontend/src/lib/api.js"
API_APP = REPO_ROOT / "services/api/app"
REPLAY_PAGE = REPO_ROOT / "frontend/src/pages/Replay.jsx"

EXPECTED = {
    ("GET", "/replay/runs"),
    ("GET", "/replay/runs/{run_id}"),
    ("GET", "/replay/runs/{run_id}/results"),
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


def test_there_is_no_write_route_and_nothing_that_starts_a_run():
    """No POST/PUT/PATCH/DELETE anywhere. The harness is a CLI batch job and
    must stay one — this is the property that keeps a research sweep off the
    trading box's CPU on someone's click."""
    assert {m for m, _p in _served()} == {"GET"}


def test_the_router_issues_no_write_statement():
    """A grant can be widened by an operator; the code must not be able to use
    it. Checked over the source so a future INSERT is a CI failure, not a row in
    a table that is supposed to be a record of what the simulator produced."""
    src = Path(R.__file__).read_text(encoding="utf-8")
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    for verb in ("insert(", "update(", "delete(", "INSERT ", "UPDATE ",
                 "DELETE ", "db.add", "db.commit"):
        assert verb not in body, f"the replay router must never {verb.strip()}"


def test_the_read_model_is_not_attached_to_the_trading_metadata():
    """The API runs `create_all` on the trading metadata at startup. A replay
    table attached to it would make the API try to CREATE in a schema it holds
    SELECT on — a crash loop AND an isolation hole."""
    from beacon_core.db.base import Base

    assert R._md is not Base.metadata
    assert set(R._md.tables) == {"replay.replay_runs", "replay.replay_results"}
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
    served = {p for _m, p in _served()}
    for p in paths:
        norm = re.sub(r"\$\{[^}]+\}", "{run_id}", p).rstrip("/")
        assert norm in served, (p, norm)


def test_the_client_has_no_call_that_could_start_a_run():
    """A POST to /replay from api.js would 404 today, but the point is that it
    must never be written — the empty state names the CLI instead."""
    js = API_JS.read_text(encoding="utf-8")
    block = js.split("replayRuns:", 1)[1].split("// brokers", 1)[0]
    for verb in ("post(", "patch(", "del("):
        assert verb not in block, f"the replay client must never {verb}"


def test_the_empty_state_names_the_command_that_produces_data():
    """Same convention as the Analysis page's excursion recompute: an empty
    state that does not say how to fill it is a dead end."""
    assert "main.py run --config" in R.RUN_COMMAND
    page = REPLAY_PAGE.read_text(encoding="utf-8")
    assert "run_command" in page


def test_the_page_renders_every_guardrail_without_interaction():
    """#169 §8 is the hard requirement, so the fields it names are asserted to
    be present in the page source rather than trusted to a reviewer's eye. This
    box has no npm — there is no render test to lean on."""
    page = REPLAY_PAGE.read_text(encoding="utf-8")
    for field in ("headline_basis", "n_variants_searched",
                  "best_of_n_inflation_sigma", "verdict_withheld",
                  "n_never_filled", "n_blocked_by_risk_limits",
                  "n_blocked_by_breaker", "n_horizon_capped",
                  "n_same_bar_ambiguous_legs", "suspect_bars_excluded",
                  "validation", "hypothesis-generating"):
        assert field in page, f"the replay page must surface {field}"


def test_the_page_has_no_control_that_starts_a_run():
    page = REPLAY_PAGE.read_text(encoding="utf-8")
    for forbidden in ("api.post", "replayStart", "startRun", "queueRun"):
        assert forbidden not in page


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
