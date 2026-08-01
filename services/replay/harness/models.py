"""The harness's OWN tables, on their OWN metadata (#169 §1).

`ReplayBase` is deliberately NOT `beacon_core.db.base.Base`. Sharing the trading
metadata would mean this service's `create_all` could create — or a future
migration could touch — `trades`, `legs`, `signals`. Isolation by construction
beats isolation by intention: with a separate declarative base, `create_all`
here is incapable of emitting DDL for a trading table, because it has never
heard of one.

Results live in a queryable table rather than a report file so aggregation
reuses the existing analysis code instead of bespoke reporting (§4).

Reproducibility columns are not decoration. `git_sha`, `variant_digest`,
`candle_digest` and `code_version` are what make "re-running a run_id reproduces
the results" a checkable claim instead of a hope: if a re-run differs, exactly
one of them changed and the diff says which.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Index, Integer,
                        Numeric, String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NUM = Numeric(18, 6)

# The harness's own schema. This is what resolves the chicken-and-egg the first
# cut of `sql/replay_role.sql` walked into: a role with only USAGE on `public`
# cannot run `create_all` (no CREATE), and the follow-up GRANT could not be
# written either, because the tables it names do not exist until that create_all
# has run.
#
# A separate schema fixes both ends AND tightens the isolation rather than
# loosening it: the alternative — granting CREATE on `public` — would let the
# harness create objects alongside the trading tables. Here it has zero CREATE
# in `public`, CREATE in `replay`, and needs no follow-up grant ever. The tables
# end up owned by the replay role because the creator owns what it creates,
# without the schema itself having to be (see the SET ROLE note in the SQL).
#
# Named `replay`, deliberately NOT `beacon_replay`: the default `search_path` is
# `"$user", public`, so a schema sharing the role's name would silently shadow
# `public` and an unqualified read of `signals` could resolve to the wrong place.
REPLAY_SCHEMA = "replay"


class ReplayBase(DeclarativeBase):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class ReplayRun(ReplayBase):
    """One execution of the harness: a signal selector, a date range, and the
    set of variants swept. `n_variants` is stored because the best of N
    backtests is upward-biased by construction — a result read without its N is
    misleading, so N is a column, not a caveat someone might forget (§8.2)."""
    __tablename__ = "replay_runs"
    __table_args__ = {"schema": REPLAY_SCHEMA}
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str | None] = mapped_column(String(96), nullable=True)
    signal_source: Mapped[str] = mapped_column(String(64), default="historical")
    symbol: Mapped[str] = mapped_column(String(16), default="XAUUSD")
    timeframe: Mapped[str] = mapped_column(String(8), default="1m")
    frm: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    to: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    holdout_from: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)     # walk-forward split (§8.1)
    n_variants: Mapped[int] = mapped_column(Integer, default=1)
    # --- reproducibility ---
    git_sha: Mapped[str | None] = mapped_column(String(48), nullable=True)
    code_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    config_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candle_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)      # the run as authored
    coverage: Mapped[dict] = mapped_column(JSON, default=dict)    # candle window + exclusions
    summary: Mapped[dict] = mapped_column(JSON, default=dict)     # per-variant reports
    # `none_as_null` is load-bearing: a plain JSON column stores Python None
    # as JSON `null`, so `validation IS NOT NULL` matched EVERY run and could
    # not distinguish "gated" from "never gated".
    validation: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True)              # §5 gate
    status: Mapped[str] = mapped_column(String(16), default="running")    # running|done|failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class ReplayResult(ReplayBase):
    """One simulated TRADE (a signal on an account under a variant), plus its
    legs as JSON.

    Trade-grained, not leg-grained, because trade-level P&L is the only
    trustworthy basis (CLAUDE.md §2.5) — leg P&L carries a known
    cross-attribution bug live, and materialising a leg-level money column here
    would invite exactly the analysis the repo has already ruled out. Leg
    OUTCOME labels are kept, since those are sound and are what
    `breakeven_leg_rate` and `pct_winners_reach_tp3` are built from."""
    __tablename__ = "replay_results"
    __table_args__ = (
        UniqueConstraint("run_id", "variant", "signal_id", "account_id",
                         name="uq_replay_result"),
        Index("ix_replay_results_run_variant", "run_id", "variant"),
        {"schema": REPLAY_SCHEMA},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey(f"{REPLAY_SCHEMA}.replay_runs.id"), index=True)
    variant: Mapped[str] = mapped_column(String(96))
    signal_id: Mapped[int] = mapped_column(Integer, index=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str] = mapped_column(String(4))
    signal_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # `taken` is false for a signal the variant did not trade; `not_taken_reason`
    # says which of filtration / caps / breaker / geometry declined it. Skipped
    # signals are ROWS, not absences — scoring what a filter rejected is the whole
    # reason the replay runs from the signal's stated entry (§6).
    taken: Mapped[bool] = mapped_column(Boolean, default=True)
    not_taken_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    entry_style: Mapped[str | None] = mapped_column(String(16), nullable=True)
    strategy_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    planned_risk: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    realized_pl: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    r_multiple: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    ever_filled: Mapped[bool] = mapped_column(Boolean, default=False)
    horizon_capped: Mapped[bool] = mapped_column(Boolean, default=False)
    same_bar_ambiguous: Mapped[int] = mapped_column(Integer, default=0)
    legs: Mapped[list] = mapped_column(JSON, default=list)
    in_sample: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


# Every table this service is allowed to write, unqualified. Asserted by the
# isolation test, so a future model added to the wrong Base — or to the wrong
# schema — fails CI rather than production.
REPLAY_TABLES = ("replay_runs", "replay_results", "replay_jobs")

# The same set as `create_all` sees them: schema-qualified.
QUALIFIED_TABLES = tuple(f"{REPLAY_SCHEMA}.{t}" for t in REPLAY_TABLES)


class ReplayJob(ReplayBase):
    """A REQUEST for a run, queued from the portal (#183).

    THE ISOLATION SPLIT THIS PRESERVES. The original design gave the platform no
    write of any kind here, on the grounds that a 400k-bar sweep must not be
    startable from a browser. What actually mattered in that argument was two
    things: a sweep must never execute inside the API process, and a stored
    RESULT must never be editable by the platform. Both still hold. What changes
    is that requesting a run is now a row, not an SSH session.

    So the grant splits: the API gets INSERT/UPDATE on THIS table only, and stays
    SELECT-only on `replay_runs` / `replay_results`. A result the platform can
    edit is not a record of what the simulator produced; a queue it can append to
    is just a queue.

    The worker — a separate, nice'd, CPU-capped process — is what executes. One
    job at a time, so a portal full of impatient clicks becomes a queue rather
    than N sweeps competing with `monitor` for the box.
    """
    __tablename__ = "replay_jobs"
    __table_args__ = (
        Index("ix_replay_jobs_status", "status", "id"),
        {"schema": REPLAY_SCHEMA},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str | None] = mapped_column(String(96), nullable=True)
    # The run config as authored, exactly the JSON the CLI takes. Same schema,
    # same validation, same `check` path — the portal is a second front door onto
    # the existing command, not a second implementation of it.
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    # queued -> running -> done | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), default="queued")
    requested_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Set by the worker when it claims the job, so a crashed worker's job is
    # visibly stuck rather than silently gone.
    claimed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # The run this job produced. Nullable until it finishes — and still nullable
    # on failure, which is why `error` is separate.
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now)


QUEUED, RUNNING, DONE, FAILED, CANCELLED = (
    "queued", "running", "done", "failed", "cancelled")
TERMINAL_JOB = (DONE, FAILED, CANCELLED)
