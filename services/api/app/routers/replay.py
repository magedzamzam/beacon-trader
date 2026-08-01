"""Read-only surface over the replay harness's results (#183).

WHAT THIS IS NOT. It is not an API on `services/replay`. That service is a batch
job behind a compose `research` profile, with no broker credentials and a
SELECT-only DB role, and that property is load-bearing — it is what keeps a
400k-bar sweep from competing with `monitor` for CPU, and it is asserted by
`services/replay/tests/test_isolation.py`. What was missing is not an HTTP
surface on the harness but a READ PATH on the one that already exists.

THE ONE RULE THAT MATTERS: **this API never executes a sweep.** It originally
had no write at all, on the grounds that a browser-startable 400k-bar replay is
a research job that can starve the trading path. The operator asked for the
portal to drive backtesting end to end, so the guarantee is now enforced by
ARCHITECTURE rather than by absence: `POST /replay/jobs` appends a row to a
queue, and a separate nice'd, CPU-capped worker claims one job at a time and
runs it. Nothing here forks, sweeps, or blocks — and a portal full of impatient
clicks becomes a queue rather than N concurrent sweeps competing with `monitor`.

The write surface is deliberately narrow: INSERT/UPDATE on `replay_jobs` only,
and SELECT-only on `replay_runs` / `replay_results`. The platform may REQUEST a
run; it may never edit what the simulator produced.

THE ONE-WAY RULE (#60 ADR). Research may read prod; prod must NEVER import
research. This module therefore reads the `replay.*` TABLES through a small
local read-model and does not `import harness.*` — reading a table is not
importing a module, and pulling the harness's models into the API would couple
every python image to research code and break the guarantee
`test_isolation.py::test_the_one_way_rule_holds_in_the_other_direction_too`
enforces. `services/api/tests/test_replay_routes.py` checks it from this side.

THE METADATA IS STANDALONE, ON PURPOSE. `_md` is a bare `MetaData()`, not
`beacon_core.db.base.Base.metadata`: the API runs `create_all` on the trading
metadata at startup, and a replay table attached to it would make the API try to
CREATE in a schema it holds SELECT on — a crash loop, and an isolation hole.
Nothing here is ever passed to `create_all`.

THE GRANT. The API connects as the trading role, which has no privilege on the
`replay` schema until an operator runs, **as `beacon_replay`** (it owns the
tables, so it is the role that can grant on them):

    GRANT USAGE ON SCHEMA replay TO <api_role>;
    GRANT SELECT ON ALL TABLES IN SCHEMA replay TO <api_role>;
    ALTER DEFAULT PRIVILEGES IN SCHEMA replay GRANT SELECT ON TABLES TO <api_role>;

SELECT and nothing else. A result the platform can edit is not a record of what
the simulator produced. Until that runs every read here fails with `permission
denied`, so the routes report `available: false` and say what to run rather than
returning a 500 the operator has to go and decode in a log.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import (JSON, Boolean, Column, DateTime, Integer, MetaData,
                        Numeric, String, Table, Text, func, select)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_token
from ..deps import get_db

router = APIRouter(prefix="/replay", tags=["replay"],
                   dependencies=[Depends(require_token)])

REPLAY_SCHEMA = "replay"

# The CLI that produces data. Quoted in the empty state so the operator is never
# left with "no runs" and no way to make one.
# Kept for the empty state and for anyone driving the harness from a shell — the
# portal is a second front door onto this command, not a replacement for it.
RUN_COMMAND = ("docker compose run --rm --no-deps replay "
               "python main.py run --config runs/<your-run>.json")
WORKER_COMMAND = "docker compose up -d replay-worker"

GRANT_SQL = (
    f"GRANT USAGE ON SCHEMA {REPLAY_SCHEMA} TO <api_role>; "
    f"GRANT SELECT ON ALL TABLES IN SCHEMA {REPLAY_SCHEMA} TO <api_role>; "
    f"ALTER DEFAULT PRIVILEGES IN SCHEMA {REPLAY_SCHEMA} "
    "GRANT SELECT ON TABLES TO <api_role>; "
    # The one write the platform gets: it may ASK for a run. Results stay
    # SELECT-only, so a stored result is never something the platform can edit.
    f"GRANT INSERT, UPDATE ON {REPLAY_SCHEMA}.replay_jobs TO <api_role>; "
    f"GRANT USAGE, SELECT ON SEQUENCE {REPLAY_SCHEMA}.replay_jobs_id_seq "
    "TO <api_role>;")

PROMOTION_BANNER = (
    "Replay results are HYPOTHESIS-GENERATING, not promotion-grade. The live "
    "frozen-week A/B/C on a frozen config remains the only thing that promotes "
    "a config (CLAUDE.md §2).")

# --- the local read-model -----------------------------------------------------
# Column-for-column with `services/replay/harness/models.py`, restated rather
# than imported (see the module docstring). Only what the read path needs.
_md = MetaData()

NUM = Numeric(18, 6)

RUNS = Table(
    "replay_runs", _md,
    Column("id", Integer, primary_key=True),
    Column("label", String(96)),
    Column("signal_source", String(64)),
    Column("symbol", String(16)),
    Column("timeframe", String(8)),
    Column("frm", DateTime(timezone=True)),
    Column("to", DateTime(timezone=True)),
    Column("holdout_from", DateTime(timezone=True)),
    Column("n_variants", Integer),
    Column("git_sha", String(48)),
    Column("code_version", String(32)),
    Column("config_digest", String(64)),
    Column("candle_digest", String(64)),
    Column("seed", Integer),
    Column("config", JSON),
    Column("coverage", JSON),
    Column("summary", JSON),
    Column("validation", JSON),
    Column("status", String(16)),
    Column("error", Text),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    schema=REPLAY_SCHEMA,
)

RESULTS = Table(
    "replay_results", _md,
    Column("id", Integer, primary_key=True),
    Column("run_id", Integer),
    Column("variant", String(96)),
    Column("signal_id", Integer),
    Column("source_id", Integer),
    Column("account_id", Integer),
    Column("symbol", String(16)),
    Column("direction", String(4)),
    Column("signal_at", DateTime(timezone=True)),
    Column("taken", Boolean),
    Column("not_taken_reason", String(48)),
    Column("entry_style", String(16)),
    Column("strategy_label", String(64)),
    Column("planned_risk", NUM),
    Column("realized_pl", NUM),
    Column("r_multiple", NUM),
    Column("ever_filled", Boolean),
    Column("horizon_capped", Boolean),
    Column("same_bar_ambiguous", Integer),
    Column("legs", JSON),
    Column("in_sample", Boolean),
    Column("created_at", DateTime(timezone=True)),
    schema=REPLAY_SCHEMA,
)


JOBS = Table(
    "replay_jobs", _md,
    Column("id", Integer, primary_key=True),
    Column("label", String(96)),
    Column("config", JSON),
    Column("status", String(16)),
    Column("requested_by", String(64)),
    Column("claimed_at", DateTime(timezone=True)),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("run_id", Integer),
    Column("error", Text),
    Column("progress", String(160)),
    Column("created_at", DateTime(timezone=True)),
    schema=REPLAY_SCHEMA,
)

# The portal may REQUEST a run. It may never execute one here, and it may never
# edit a stored result — see the module docstring. These bound what a request is
# allowed to ask for, so a browser cannot queue an unbounded sweep.
MAX_QUEUED = 5
MAX_VARIANTS = 24


def _unavailable(exc: Exception) -> dict:
    """The grant hasn't been run (or the harness has never created its tables).

    Reported as a first-class state rather than a 500: it is the expected
    condition on a box where replay has not been set up, it has exactly one
    fix, and that fix is a SQL statement nobody can guess from a stack trace."""
    return {
        "available": False,
        "reason": str(getattr(exc, "orig", exc))[:300],
        "grant_sql": GRANT_SQL,
        "run_command": RUN_COMMAND,
        "note": ("The API role holds no privilege on the `replay` schema yet. "
                 "Run the grant as `beacon_replay` (it owns the tables) — SELECT "
                 "only; the platform must never be able to write a replay row. "
                 "See services/replay/sql/replay_role.sql."),
    }


async def _rollback(db: AsyncSession) -> None:
    """A failed SELECT aborts the transaction; without this the next statement on
    the same session fails with `InFailedSQLTransaction` and the error the
    operator sees is the second one, not the one that matters."""
    try:
        await db.rollback()
    except SQLAlchemyError:
        pass


def _iso(v):
    return v.isoformat() if v is not None else None


def _num(v):
    return None if v is None else float(v)


def _headline_of(summary: dict) -> dict:
    """The guardrails the run picker must carry, folded up per run.

    A list row that shows only a label and a date invites the operator to open
    the best-sounding one; these are the fields that say whether there is
    anything to open. `basis` is the whole ballgame — an in-sample-only run is a
    description of the past, not an edge (#169 §8.1)."""
    variants = (summary or {}).get("variants") or {}
    ranking = (summary or {}).get("ranking") or []
    bases = {v.get("headline_basis") for v in variants.values() if isinstance(v, dict)}
    guards = [(v.get("guardrails") or {}) for v in variants.values() if isinstance(v, dict)]
    searched = max([int(g.get("n_variants_searched") or 1) for g in guards] or [1])
    sigmas = [g.get("best_of_n_inflation_sigma") for g in guards
              if g.get("best_of_n_inflation_sigma") is not None]
    return {
        # held_out beats in_sample: if ANY arm is in-sample only, say so loudly.
        "headline_basis": ("in_sample" if "in_sample" in bases
                           else ("held_out" if "held_out" in bases else None)),
        "n_variants_searched": searched,
        "best_of_n_inflation_sigma": max(sigmas) if sigmas else None,
        # True when EVERY ranked variant is below the N>=30 floor — i.e. the
        # whole run is unreadable as a ranking, not merely one row of it.
        "all_verdicts_withheld": (bool(ranking) and
                                  all(bool(r.get("verdict_withheld")) for r in ranking)),
        "n_verdicts_withheld": sum(1 for r in ranking if r.get("verdict_withheld")),
        "n_ranked": len(ranking),
    }


async def _validation_index(db: AsyncSession) -> dict:
    """`(git_sha, candle_digest)` -> the newest run carrying a gate verdict.

    THE GATE VALIDATES THE SIMULATOR, NOT A SWEEP. What it establishes is that
    a given version of the code, replayed over a given set of bars, reproduces
    what the broker actually did. A sweep on that same code and those same bars
    is covered by it — so reporting "not validated" there is simply false, and it
    was: every stored sweep showed an amber unvalidated banner while a passing
    gate sat in the same table.

    Reporting it as the sweep's OWN result would be equally false, so an
    inherited verdict is labelled as inherited and names the run it came from.
    Matched on `git_sha` AND `candle_digest` together: different code, or
    different bars, is a different simulator and inherits nothing."""
    q = (select(RUNS.c.id, RUNS.c.git_sha, RUNS.c.candle_digest, RUNS.c.validation)
         .order_by(RUNS.c.id.desc()).limit(200))
    idx: dict = {}
    for r in (await db.execute(q)).all():
        # NOT `WHERE validation IS NOT NULL`. A plain JSON column stores Python
        # None as JSON `null`, so that predicate matched every row ever written
        # and the index filled up with runs that carry no verdict at all. The
        # shape is what qualifies a row, checked here so rows written before
        # `none_as_null` was set are handled the same as rows written after.
        val = r.validation if isinstance(r.validation, dict) else None
        if not val or not isinstance(val.get("gate"), dict):
            continue
        if not r.git_sha or not r.candle_digest:
            continue                    # unattributable — cannot be inherited from
        idx.setdefault((r.git_sha, r.candle_digest), r)   # newest first, so first wins
    return idx


def _validation_of(run, index: dict = None) -> dict:
    """The §5 gate verdict, flattened.

    `ran: false` is NOT `passed: false` — one is "nobody checked the simulator
    against broker truth", the other is "the check failed". Both mean the
    counterfactual is not actionable, and the UI has to be able to say which."""
    val = run.validation or None
    source, from_run = ("own", run.id) if val is not None else (None, None)
    if val is None and index:
        hit = index.get((run.git_sha, run.candle_digest))
        if hit is not None and hit.id != run.id:
            val, source, from_run = hit.validation, "inherited", hit.id
    gate = (val or {}).get("gate") or {}
    return {
        "ran": val is not None,
        "source": source,               # own | inherited | None
        "from_run_id": from_run,
        "passed": bool(gate.get("passed")) if val is not None else None,
        "failures": gate.get("failures") or [],
        "systematic_bias": gate.get("systematic_bias"),
    }


def _run_row(r, index: dict = None) -> dict:
    return {
        "id": r.id, "label": r.label, "signal_source": r.signal_source,
        "symbol": r.symbol, "timeframe": r.timeframe,
        "from": _iso(r.frm), "to": _iso(r.to),
        "holdout_from": _iso(r.holdout_from),
        "n_variants": r.n_variants,
        "git_sha": r.git_sha, "code_version": r.code_version,
        "config_digest": r.config_digest, "candle_digest": r.candle_digest,
        "status": r.status, "error": r.error,
        "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at),
        "validation": _validation_of(r, index),
        **_headline_of(r.summary or {}),
    }


@router.post("/jobs", status_code=202)
async def enqueue_job(body: dict, db: AsyncSession = Depends(get_db),
                      user=Depends(require_token)):
    """Queue a replay run. Returns 202 — ACCEPTED, not executed.

    THIS ROUTE DOES NOT RUN ANYTHING. It appends a row; a separate nice'd,
    CPU-capped worker claims it and runs one job at a time. That is what keeps
    the original guarantee intact: a 400k-bar sweep still never executes inside
    the API process, and a portal full of impatient clicks becomes a queue
    rather than N sweeps competing with `monitor` for the box that manages open
    positions.

    The config is the SAME JSON the CLI takes, so the portal is a second front
    door onto the existing command rather than a second implementation of it."""
    cfg = (body or {}).get("config")
    if not isinstance(cfg, dict) or not cfg:
        raise HTTPException(400, "config must be a non-empty object")
    variants = cfg.get("variants")
    if not isinstance(variants, list) or not variants:
        raise HTTPException(400, "config.variants must be a non-empty list")
    if len(variants) > MAX_VARIANTS:
        # Best-of-N is upward-biased by construction; an unbounded grid from a
        # browser is a false-discovery machine with a submit button.
        raise HTTPException(400, f"at most {MAX_VARIANTS} variants per run "
                                 f"(asked for {len(variants)})")
    try:
        queued = int((await db.execute(
            select(func.count()).select_from(JOBS)
            .where(JOBS.c.status.in_(("queued", "running"))))).scalar() or 0)
        if queued >= MAX_QUEUED:
            raise HTTPException(429, f"{queued} jobs already queued or running "
                                     f"(limit {MAX_QUEUED}) — wait for the worker")
        res = await db.execute(JOBS.insert().values(
            label=str((body or {}).get("label") or cfg.get("label") or "")[:96] or None,
            config=cfg, status="queued",
            requested_by=str(getattr(user, "username", None) or "portal")[:64],
        ).returning(JOBS.c.id))
        job_id = res.scalar_one()
        await db.commit()
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        await _rollback(db)
        return {**_unavailable(exc), "job_id": None}
    return {"available": True, "job_id": job_id, "status": "queued",
            "note": ("Queued. A separate worker runs it — nothing executes in "
                     "the API. Poll GET /replay/jobs for progress.")}


@router.get("/jobs")
async def list_jobs(limit: int = 25, db: AsyncSession = Depends(get_db)):
    """The queue, newest first, so the portal can show progress and failures."""
    limit = max(1, min(int(limit or 25), 100))
    try:
        rows = (await db.execute(select(JOBS).order_by(JOBS.c.id.desc())
                                 .limit(limit))).all()
    except SQLAlchemyError as exc:
        await _rollback(db)
        return {**_unavailable(exc), "jobs": []}
    return {"available": True, "jobs": [{
        "id": r.id, "label": r.label, "status": r.status,
        "requested_by": r.requested_by, "run_id": r.run_id,
        "progress": r.progress, "error": r.error,
        "created_at": _iso(r.created_at), "started_at": _iso(r.started_at),
        "finished_at": _iso(r.finished_at),
        "n_variants": len((r.config or {}).get("variants") or []),
    } for r in rows]}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: int, db: AsyncSession = Depends(get_db)):
    """Cancel a job that has not started. A RUNNING job is left alone — killing
    a sweep mid-write would leave a half-populated run, and the worker's stale-job
    reaper is what handles a worker that dies."""
    try:
        row = (await db.execute(select(JOBS).where(JOBS.c.id == job_id))).first()
        if row is None:
            raise HTTPException(404, f"job {job_id} not found")
        if row.status != "queued":
            raise HTTPException(409, f"job {job_id} is {row.status}; only a "
                                     "queued job can be cancelled")
        await db.execute(JOBS.update().where(JOBS.c.id == job_id)
                         .values(status="cancelled"))
        await db.commit()
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        await _rollback(db)
        return _unavailable(exc)
    return {"available": True, "job_id": job_id, "status": "cancelled"}


@router.get("/runs")
async def list_runs(limit: int = 50, offset: int = 0,
                    db: AsyncSession = Depends(get_db)):
    """Every stored run, newest first, each carrying the guardrails a reader
    needs BEFORE opening it: held-out vs in-sample, how many variants were
    searched, how many verdicts are withheld, and whether the validation gate
    ran and passed."""
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    q = (select(RUNS).order_by(RUNS.c.id.desc()).limit(limit).offset(offset))
    try:
        rows = (await db.execute(q)).all()
        total = int((await db.execute(
            select(func.count()).select_from(RUNS))).scalar() or 0)
        index = await _validation_index(db)
    except SQLAlchemyError as exc:
        await _rollback(db)
        return {**_unavailable(exc), "runs": [], "total": 0}
    return {"available": True, "total": total, "limit": limit, "offset": offset,
            "runs": [_run_row(r, index) for r in rows],
            "run_command": RUN_COMMAND, "promotion": PROMOTION_BANNER}


@router.get("/runs/{run_id}")
async def get_run(run_id: int, db: AsyncSession = Depends(get_db)):
    """One run in full: the stored `summary` (per-variant reports, `by_source`,
    `caveats`, `guardrails`, `ranking`) plus `coverage` and `validation`.

    Served whole rather than pre-digested. The harness deliberately emits the
    same `geometry_ab_rollup` keys as a live arm so an offline result reads like
    a live one, and re-shaping it here would undo that."""
    try:
        row = (await db.execute(
            select(RUNS).where(RUNS.c.id == run_id))).first()
        index = await _validation_index(db) if row is not None else {}
    except SQLAlchemyError as exc:
        await _rollback(db)
        return {**_unavailable(exc), "run": None}
    if row is None:
        raise HTTPException(404, f"replay run {run_id} not found")
    summary = row.summary or {}
    return {
        "available": True,
        "run": _run_row(row, index),
        "config": row.config or {},
        "coverage": row.coverage or {},
        "validation": row.validation,
        "summary": summary,
        "variants": summary.get("variants") or {},
        "ranking": summary.get("ranking") or [],
        "promotion": PROMOTION_BANNER,
    }


@router.get("/runs/{run_id}/results")
async def get_run_results(run_id: int, variant: str = None, source_id: int = None,
                          taken: bool = None, not_taken_reason: str = None,
                          limit: int = 200, offset: int = 0,
                          db: AsyncSession = Depends(get_db)):
    """Paged rows from `replay.replay_results`.

    Declined signals are ROWS here, not absences — a variant is defined as much
    by what it refuses as by what it takes, and `not_taken_reason` is the only
    way to see what a filter rejected. Hence the filters, and hence
    `by_reason`: it is the drill-down the page hangs off."""
    limit = max(1, min(int(limit or 200), 1000))
    offset = max(0, int(offset or 0))
    q = select(RESULTS).where(RESULTS.c.run_id == run_id)
    if variant:
        q = q.where(RESULTS.c.variant == variant)
    if source_id is not None:
        q = q.where(RESULTS.c.source_id == source_id)
    if taken is not None:
        q = q.where(RESULTS.c.taken == taken)
    if not_taken_reason:
        q = q.where(RESULTS.c.not_taken_reason == not_taken_reason)
    try:
        rows = (await db.execute(
            q.order_by(RESULTS.c.id).limit(limit).offset(offset))).all()
        total = int((await db.execute(
            select(func.count()).select_from(q.subquery()))).scalar() or 0)
        # Scoped to the same variant as the rows. A breakdown counted over the
        # whole run beside rows filtered to one variant would put a number on a
        # chip that does not match what clicking it returns.
        rq = (select(RESULTS.c.not_taken_reason, func.count())
              .where(RESULTS.c.run_id == run_id, RESULTS.c.taken.is_(False)))
        if variant:
            rq = rq.where(RESULTS.c.variant == variant)
        reasons = (await db.execute(
            rq.group_by(RESULTS.c.not_taken_reason))).all()
    except SQLAlchemyError as exc:
        await _rollback(db)
        return {**_unavailable(exc), "rows": [], "total": 0}
    return {
        "available": True, "run_id": run_id, "total": total,
        "limit": limit, "offset": offset,
        "by_reason": {str(r[0]): int(r[1]) for r in reasons},
        "rows": [{
            "id": r.id, "variant": r.variant, "signal_id": r.signal_id,
            "source_id": r.source_id, "account_id": r.account_id,
            "symbol": r.symbol, "direction": r.direction,
            "signal_at": _iso(r.signal_at),
            "taken": bool(r.taken), "not_taken_reason": r.not_taken_reason,
            "entry_style": r.entry_style, "strategy_label": r.strategy_label,
            "planned_risk": _num(r.planned_risk),
            "realized_pl": _num(r.realized_pl),
            "r_multiple": _num(r.r_multiple),
            "ever_filled": bool(r.ever_filled),
            "horizon_capped": bool(r.horizon_capped),
            "same_bar_ambiguous": r.same_bar_ambiguous,
            "in_sample": bool(r.in_sample),
            "legs": r.legs or [],
        } for r in rows],
    }
