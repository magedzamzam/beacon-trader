"""The DB half: read trading data, write only `replay_*` (#169 §1).

ISOLATION, in code as well as in grants:

  * Its own engine, from `REPLAY_DATABASE_URL` — NOT `DATABASE_URL`. The
    container is given a SELECT-only role (see `sql/replay_role.sql`); the code
    never sees the trading DSN, so a bug cannot promote itself to a write.
  * `create_all` runs on `ReplayBase.metadata` only, which contains exactly two
    tables — both in the `replay` SCHEMA the role owns — and has never been told
    a trading table exists. The role holds no CREATE in `public` whatsoever.
  * No queue participation: nothing here touches `bus`, `consume_queue` or
    `CH_SIGNAL_VALID`. The executor's durable queue is not a place a research
    job belongs, and dropping a message off it would be indistinguishable from
    a lost trade.

The candle read is deliberately NOT from the daily SQL dump: candles are
excluded from `pg_backup.ps1` (they added 83MB and broke the integrity loop), so
the dump has none. Bars come from the live/replica DB, read-only.
"""
from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (AsyncSession, async_sessionmaker,
                                    create_async_engine)

from beacon_core.db.models import (Account, AccountSourceRisk, Candle,
                                   ExecutionStrategy, Leg, Signal, Source,
                                   SymbolMap, Trade)
from beacon_core.parsing.models import ParsedSignal
from beacon_core.settings_store import get_setting

from . import bars as B
from . import scaffold
from .models import REPLAY_SCHEMA, ReplayBase, ReplayResult, ReplayRun
from .portfolio import SignalRow
from .variants import canonical_digest

# The SELECT-only DSN. Falling back to DATABASE_URL would quietly re-couple the
# harness to the trading role, so a missing value is an ERROR, not a default.
DSN_ENV = "REPLAY_DATABASE_URL"

_engine = None
_Session: Optional[async_sessionmaker] = None


def dsn() -> str:
    url = os.getenv(DSN_ENV)
    if not url:
        raise RuntimeError(
            f"{DSN_ENV} is not set. The replay service must connect with its own "
            "SELECT-only role — it deliberately does not fall back to "
            "DATABASE_URL (see sql/replay_role.sql).")
    return url


def engine():
    global _engine
    if _engine is None:
        # Connection-capped: a wide sweep must not starve `monitor`, which manages
        # open positions on a tick loop against the same database.
        _engine = create_async_engine(dsn(), pool_pre_ping=True, pool_size=2,
                                      max_overflow=2)
    return _engine


def Session() -> async_sessionmaker:
    global _Session
    if _Session is None:
        _Session = async_sessionmaker(engine(), expire_on_commit=False,
                                      class_=AsyncSession)
    return _Session


async def init_replay_tables() -> None:
    """Create `replay.replay_runs` / `replay.replay_results` if absent. Only ever
    emits DDL for this service's own metadata — see the module docstring.

    The SCHEMA is not created here. Creating one needs CREATE on the DATABASE,
    which the replay role deliberately does not hold; the operator makes it once
    and grants CREATE on it. The two failure modes are distinguished, because
    they have different fixes and both otherwise surface as an opaque
    `permission denied` from three frames inside SQLAlchemy.

    Read from `pg_namespace`, not `information_schema.schemata`: that view is
    filtered to schemas the current user owns or has a privilege on, so it
    cannot tell "the schema is missing" from "the schema exists and I was not
    granted anything on it" — which is exactly the distinction being made."""
    async with engine().begin() as conn:
        row = (await conn.exec_driver_sql(
            "SELECT has_schema_privilege(current_user, n.oid, 'CREATE') "
            f"FROM pg_namespace n WHERE n.nspname = '{REPLAY_SCHEMA}'")).first()
        if row is None:
            raise RuntimeError(
                f"schema '{REPLAY_SCHEMA}' does not exist. The harness writes "
                "only there and holds no CREATE in public, so an operator has to "
                "make it once. As the database owner:\n\n"
                f"    CREATE SCHEMA {REPLAY_SCHEMA};\n"
                f"    GRANT USAGE, CREATE ON SCHEMA {REPLAY_SCHEMA} "
                "TO beacon_replay;\n\n"
                "See services/replay/sql/replay_role.sql.")
        if not row[0]:
            raise RuntimeError(
                f"schema '{REPLAY_SCHEMA}' exists but this role cannot create in "
                "it. As the database owner:\n\n"
                f"    GRANT USAGE, CREATE ON SCHEMA {REPLAY_SCHEMA} "
                "TO beacon_replay;\n\n"
                "See services/replay/sql/replay_role.sql.")
        await conn.run_sync(ReplayBase.metadata.create_all)


# --- reads --------------------------------------------------------------------
_BAR_COLS = (Candle.ts, Candle.open_bid, Candle.open_ask, Candle.high_bid,
             Candle.high_ask, Candle.low_bid, Candle.low_ask, Candle.close_bid,
             Candle.close_ask, Candle.volume)


async def load_series(session, *, symbol: str = "XAUUSD", timeframe: str = "1m",
                      frm=None, to=None) -> B.BarSeries:
    """Usable bars, ascending. `quality != 'ok'` is EXCLUDED — a crossed quote or
    an outlier bar prints a phantom wick that would falsely trigger a TP or an SL
    — and the excluded COUNT rides on every result as a caveat (#169: flag, never
    auto-repair; there is no way to tell from the row which side is wrong)."""
    q = (select(*_BAR_COLS)
         .where(Candle.symbol == symbol, Candle.timeframe == timeframe,
                Candle.quality == "ok"))
    if frm is not None:
        q = q.where(Candle.ts >= frm)
    if to is not None:
        q = q.where(Candle.ts < to)
    rows = (await session.execute(q.order_by(Candle.ts))).all()
    bars = [B.Bar(ts=r[0], open_bid=_f(r[1]), open_ask=_f(r[2]),
                  high_bid=_f(r[3]), high_ask=_f(r[4]), low_bid=_f(r[5]),
                  low_ask=_f(r[6]), close_bid=_f(r[7]), close_ask=_f(r[8]),
                  volume=None if r[9] is None else _f(r[9])) for r in rows]
    suspect = await suspect_count(session, symbol, timeframe, frm=frm, to=to)
    return B.BarSeries(bars, symbol=symbol, timeframe=timeframe,
                       suspect_excluded=suspect)


async def suspect_count(session, symbol: str, timeframe: str, frm=None, to=None) -> int:
    q = (select(func.count()).select_from(Candle)
         .where(Candle.symbol == symbol, Candle.timeframe == timeframe,
                Candle.quality != "ok"))
    if frm is not None:
        q = q.where(Candle.ts >= frm)
    if to is not None:
        q = q.where(Candle.ts < to)
    return int((await session.execute(q)).scalar() or 0)


async def candle_digest(session, *, symbol: str, timeframe: str, frm=None,
                        to=None) -> str:
    """A cheap fingerprint of the candle data a run read: row count and window.
    Recorded on the run so 'the bars changed' is distinguishable from 'the code
    changed' when a re-run does not reproduce."""
    q = (select(func.count(), func.min(Candle.ts), func.max(Candle.ts))
         .where(Candle.symbol == symbol, Candle.timeframe == timeframe,
                Candle.quality == "ok"))
    if frm is not None:
        q = q.where(Candle.ts >= frm)
    if to is not None:
        q = q.where(Candle.ts < to)
    n, lo, hi = (await session.execute(q)).one()
    return canonical_digest({"symbol": symbol, "timeframe": timeframe,
                             "n": int(n or 0),
                             "first": lo.isoformat() if lo else None,
                             "last": hi.isoformat() if hi else None})


async def load_signals(session, *, symbol: str = "XAUUSD", frm=None, to=None,
                       source_ids=None, account_ids=None,
                       strict_accounts: bool = False) -> List[SignalRow]:
    """Every signal in the window as a `SignalRow` — including ones we skipped,
    filtered, risk-blocked or never filled. That is the §6 requirement: replaying
    from the signal's STATED entry takes the evaluable set from the filled trades
    to the whole signal history, and it is the only way to score what a filter
    rejected."""
    q = select(Signal).where(Signal.symbol == symbol)
    if frm is not None:
        q = q.where(Signal.created_at >= frm)
    if to is not None:
        q = q.where(Signal.created_at < to)
    if source_ids:
        q = q.where(Signal.source_id.in_(list(source_ids)))
    rows = (await session.execute(q.order_by(Signal.id))).scalars().all()

    srcs = {s.id: s for s in (await session.execute(select(Source))).scalars().all()}
    out: List[SignalRow] = []
    for s in rows:
        tps = []
        for t in (s.tps or []):
            try:
                tps.append(Decimal(str(t)))
            except (ArithmeticError, TypeError, ValueError):
                continue
        if not tps:
            continue
        src = srcs.get(s.source_id)
        mapped = tuple(src.account_map or ()) if src else ()
        if account_ids:
            mapped = tuple(a for a in mapped if a in set(account_ids))
            if not mapped:
                # A sweep scoped to an account wants the signal evaluated THERE
                # regardless of the routing table, so the default fails open.
                # A what-if asking "what did this account actually do" wants the
                # opposite: a channel that never reached the account did not
                # contribute to its P&L, and pulling it in answers a different
                # question than the one that was asked.
                if strict_accounts:
                    continue
                mapped = tuple(account_ids)
        out.append(SignalRow(
            id=s.id, at=s.created_at, source_id=s.source_id,
            source_name=src.name if src else None, account_ids=mapped,
            parsed=ParsedSignal(
                symbol=s.symbol, direction=s.direction,
                entry_from=Decimal(str(s.entry_from)),
                entry_to=Decimal(str(s.entry_to)), sl=Decimal(str(s.sl)),
                tps=tps, order_type_hint=s.order_type, raw_text=s.raw_text or "")))
    return out


async def load_live_truth(session, *, symbol: str = "XAUUSD", frm=None, to=None) -> dict:
    """Broker-truth rows for the validation gate: per-leg fill/outcome and
    per-trade R.

    R uses `trades.realized_pl / trades.planned_risk` — trade-level P&L is
    trustworthy, leg-level is not (CLAUDE.md §2.5), so the leg rows carry outcome
    LABELS only and no money."""
    q = (select(Trade.signal_id, Trade.account_id, Trade.realized_pl,
                Trade.planned_risk, Trade.direction, Leg.tp_index,
                Leg.fill_price, Leg.outcome)
         .join(Leg, Leg.trade_id == Trade.id)
         .where(Trade.symbol == symbol))
    if frm is not None:
        q = q.where(Trade.created_at >= frm)
    if to is not None:
        q = q.where(Trade.created_at < to)
    legs, trades, seen = [], [], set()
    for sid, aid, pl, risk, direction, tp_index, fill, outcome in (
            await session.execute(q.order_by(Trade.signal_id, Leg.id))).all():
        # `direction` rides along so the fill difference can be re-signed into
        # "who got the worse entry" — pooled across BUY and SELL it is not a
        # bias, and a systematic entry advantage would average to nothing.
        legs.append({"signal_id": sid, "account_id": aid, "tp_index": tp_index,
                     "direction": direction,
                     "fill_price": None if fill is None else float(fill),
                     "outcome": outcome})
        if (sid, aid) in seen:
            continue
        seen.add((sid, aid))
        r = None
        if pl is not None and risk not in (None, 0) and float(risk) != 0:
            r = float(pl) / abs(float(risk))
        trades.append({"signal_id": sid, "account_id": aid, "r": r})
    return {"legs": legs, "trades": trades}


async def load_accounts(session) -> List[dict]:
    rows = (await session.execute(select(Account))).scalars().all()
    return [{"id": a.id, "name": a.name, "currency": a.currency,
             "enabled": bool(a.enabled),
             "risk_config": dict(a.risk_config or {})} for a in rows]


async def load_live_config(session, *, symbol: str = "XAUUSD", equity,
                           frm=None, to=None, holdout_from=None) -> dict:
    """Read the live execution config and emit it as a run config (§5).

    Everything here is a SELECT on a trading table. The assembly is
    `scaffold.build_run_config`, kept pure so what the JSON says about the live
    config is unit-testable without a database."""
    accounts = [a for a in await load_accounts(session) if a["enabled"]]
    sources = [{"id": s.id, "name": s.name,
                "account_map": list(s.account_map or []),
                "strategy": dict(s.strategy or {})}
               for s in (await session.execute(
                   select(Source).where(Source.archived == False))  # noqa: E712
               ).scalars().all()]
    strategies = [{"id": s.id, "account_id": s.account_id, "source_id": s.source_id,
                   "entry_policy": dict(s.entry_policy or {}),
                   "entry_filters": dict(s.entry_filters or {}),
                   "exit_policy": dict(s.exit_policy or {}),
                   "enabled": bool(s.enabled), "label": s.label}
                  for s in (await session.execute(
                      select(ExecutionStrategy))).scalars().all()]
    asr = [{"account_id": r.account_id, "source_id": r.source_id,
            "risk_config": dict(r.risk_config or {}), "enabled": bool(r.enabled)}
           for r in (await session.execute(
               select(AccountSourceRisk))).scalars().all()]
    limits = await get_setting(session, "risk_limits", None)
    # The session windows (#81). Read raw rather than through
    # `th_service.load_config`, which sanitises AND would pull in the econ
    # calendar the harness does not model — copying that in would imply it does.
    hours = await get_setting(session, "trading_hours", None)
    smap = await load_symbol_map(session, symbol)
    return scaffold.build_run_config(
        accounts=accounts, sources=sources, strategies=strategies,
        account_source_risk=asr, risk_limits=limits, symbol_map=smap,
        trading_hours=hours, equity=equity, symbol=symbol, frm=frm, to=to,
        holdout_from=holdout_from)


async def load_symbol_map(session, symbol: str = "XAUUSD") -> Optional[dict]:
    row = (await session.execute(select(SymbolMap).where(
        SymbolMap.internal_symbol == symbol))).scalars().first()
    if row is None:
        return None
    return {"value_per_point": str(row.value_per_point), "min_lot": str(row.min_lot),
            "lot_step": str(row.lot_step),
            "min_stop_distance": None if row.min_stop_distance is None
            else str(row.min_stop_distance)}


async def load_sources(session) -> dict:
    return {s.id: s.name for s in
            (await session.execute(select(Source))).scalars().all()}


# --- writes (replay_* ONLY) ---------------------------------------------------
async def create_run(session, **kw) -> ReplayRun:
    run = ReplayRun(**kw)
    session.add(run)
    await session.flush()
    return run


async def finish_run(session, run: ReplayRun, *, summary: dict, coverage: dict,
                     validation=None, status: str = "done",
                     error: Optional[str] = None) -> None:
    run.summary = summary
    run.coverage = coverage
    run.validation = validation
    run.status = status
    run.error = error
    run.finished_at = dt.datetime.now(dt.timezone.utc)
    await session.commit()


def result_rows(run_id: int, variant_name: str, res, *, holdout_from=None) -> List[dict]:
    """Flatten one variant's outcome into `replay_results` rows — taken trades
    AND the signals it declined, because a variant is defined as much by what it
    refuses as by what it takes."""
    out: List[dict] = []
    for t in res.trades:
        risk = float(t.planned_risk or 0)
        pl = float(t.realized_pl)
        out.append({
            "run_id": run_id, "variant": variant_name, "signal_id": t.signal_id,
            "source_id": t.source_id, "account_id": t.account_id,
            "symbol": t.symbol, "direction": t.direction, "signal_at": t.signal_at,
            "taken": True, "not_taken_reason": None, "entry_style": t.entry_style,
            "strategy_label": t.strategy_label,
            "planned_risk": Decimal(str(risk)), "realized_pl": Decimal(str(pl)),
            "r_multiple": (Decimal(str(round(pl / abs(risk), 6)))
                           if risk else None),
            "ever_filled": t.ever_filled, "horizon_capped": t.horizon_capped,
            "same_bar_ambiguous": t.same_bar_ambiguous,
            "in_sample": bool(holdout_from is None or t.signal_at < holdout_from),
            "legs": [{"tp_index": l.tp_index, "tranche": l.tranche,
                      "order_type": l.order_type, "status": l.status,
                      "entry": l.entry, "tp": l.tp, "sl": l.sl,
                      "fill_price": l.fill_price, "close_price": l.close_price,
                      "outcome": l.outcome, "sl_moved": l.sl_moved,
                      "same_bar_ambiguous": l.same_bar_ambiguous}
                     for l in t.legs],
        })
    for nt in res.not_taken:
        out.append({
            "run_id": run_id, "variant": variant_name,
            "signal_id": nt.get("signal_id"), "source_id": nt.get("source_id"),
            "account_id": nt.get("account_id"), "symbol": "", "direction": "",
            "signal_at": None, "taken": False,
            "not_taken_reason": str(nt.get("reason"))[:48],
            "ever_filled": False, "legs": [], "in_sample": True,
        })
    return out


async def write_results(session, rows: List[dict]) -> int:
    for r in rows:
        session.add(ReplayResult(**r))
    await session.commit()
    return len(rows)


def sim_legs_for_validation(res) -> tuple:
    """(leg rows, trade rows) from a simulated run, in the shape `validate.report`
    compares against `load_live_truth`."""
    legs, trades = [], []
    for t in res.trades:
        risk = float(t.planned_risk or 0)
        trades.append({"signal_id": t.signal_id, "account_id": t.account_id,
                       "r": (float(t.realized_pl) / abs(risk)) if risk else None,
                       # #185: a trade whose entries never filled settles at
                       # realized_pl = 0, so it enters the R comparison as a
                       # HONEST-LOOKING 0.0R rather than as absent. The gate has
                       # to be able to tell those apart from a trade that really
                       # scratched, or an under-fill reads as a flat outcome.
                       "ever_filled": t.ever_filled})
        for l in t.legs:
            legs.append({"signal_id": t.signal_id, "account_id": t.account_id,
                         "tp_index": l.tp_index, "direction": t.direction,
                         "fill_price": l.fill_price, "outcome": l.outcome,
                         # #185: how near this order came to filling, and
                         # whether the TTL retired it on a fillable bar. Carried
                         # into the gate so "sim expired, live filled" can be
                         # explained rather than counted.
                         "order_type": l.order_type, "entry": l.entry,
                         "closest_approach": l.closest_approach,
                         "expired_on_fillable_bar": l.expired_on_fillable_bar,
                         # #190: what the "fills AT its level, never better" rule
                         # cost on this fill. Carried so `fill_adverse` can be
                         # split into the harness's own rule and the residual,
                         # rather than the whole of it being read as spread.
                         "gap_at_open": l.gap_at_open})
    return legs, trades


def _f(v) -> float:
    return float(v)


# --- the job queue (#183) -----------------------------------------------------
# The portal appends here; the worker drains it. The API holds INSERT/UPDATE on
# THIS table and SELECT-only on runs/results, so the platform can request a run
# and can still never edit what the simulator produced.
async def claim_next_job(session):
    """Claim the oldest queued job, or None.

    `SKIP LOCKED` so two workers can never take the same job — one worker is the
    intended deployment, but a queue whose correctness depends on there being
    exactly one consumer is a queue that breaks the first time someone scales it
    or restarts during a run."""
    from .models import QUEUED, RUNNING, ReplayJob
    row = (await session.execute(
        select(ReplayJob).where(ReplayJob.status == QUEUED)
        .order_by(ReplayJob.id).limit(1).with_for_update(skip_locked=True))
    ).scalars().first()
    if row is None:
        return None
    row.status = RUNNING
    row.claimed_at = dt.datetime.now(dt.timezone.utc)
    row.started_at = row.claimed_at
    await session.commit()
    return row


async def finish_job(session, job, *, status, run_id=None, error=None,
                     progress=None) -> None:
    job.status = status
    job.run_id = run_id
    job.error = (error or None) and str(error)[:2000]
    job.progress = progress
    job.finished_at = dt.datetime.now(dt.timezone.utc)
    await session.commit()


async def set_job_progress(session, job, message: str) -> None:
    """A long sweep that reports nothing is indistinguishable from a hung one."""
    job.progress = (message or "")[:160]
    await session.commit()


async def requeue_stale_running_jobs(session, *, older_than_minutes: int = 120) -> int:
    """A job left RUNNING by a killed worker is stuck forever otherwise.

    Marked FAILED rather than re-queued: the sweep may have written a partial
    run, and silently retrying something that already half-executed is how you
    get two runs claiming to be the same job."""
    from .models import FAILED, RUNNING, ReplayJob
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=older_than_minutes)
    rows = (await session.execute(select(ReplayJob).where(
        ReplayJob.status == RUNNING, ReplayJob.claimed_at < cutoff))).scalars().all()
    for r in rows:
        r.status = FAILED
        r.error = (f"worker died or stalled: claimed at {r.claimed_at} and still "
                   f"RUNNING after {older_than_minutes} minutes")
        r.finished_at = dt.datetime.now(dt.timezone.utc)
    if rows:
        await session.commit()
    return len(rows)
