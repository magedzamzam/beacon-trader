"""services/replay — the offline signal-replay + backtest harness (#169).

A BATCH JOB, not a daemon. There is no loop, no queue consumer and no HTTP
server, because the safest way to be incapable of disturbing live trading is to
not be running. It is invoked explicitly, and in this order:

    docker compose build replay                               # code is COPYed in
    docker compose run --rm --no-deps replay python main.py init
    docker compose run --rm --no-deps replay python main.py coverage
    docker compose run --rm --no-deps replay python main.py scaffold --equity 10000
    docker compose run --rm --no-deps replay python main.py validate --config runs/live-config.json
    docker compose run --rm --no-deps replay python main.py run --config runs/exit.json

`--no-deps` every time: the service declares no `depends_on`, so there is
nothing to start — but `run` is a command that CAN start other containers, and
this one must never be the reason a trading service changes state.

`init` creates the two `replay.*` tables. It exists so the first proof that the
DB grant works costs two seconds rather than a completed sweep — every other
thing this service does to the database is a SELECT, so it is the only write
worth rehearsing. `run` performs the same init BEFORE it simulates anything, for
the same reason.

`docker compose stop replay` is a no-op on api/executor/monitor/telegram by
construction: nothing depends on this service, it holds no broker credentials,
it never touches the executor's durable queue, and it connects with a
SELECT-only role (`sql/replay_role.sql`) that can write only `replay_*`.

RUN CONFIG (JSON)
-----------------
    {
      "label": "exit-ladder sweep",
      "symbol": "XAUUSD",
      "from": "2026-07-05T00:00:00Z",
      "holdout_from": "2026-07-20T00:00:00Z",
      "signal_source": "historical",
      "workers": 0,
      "defaults":  { ...variant keys shared by every variant... },
      "variants":  [ { "name": "be_at_tp1", ... }, { "name": "be_at_tp2", ... } ]
    }

`defaults` is merged UNDER each variant (the variant wins) so a sweep expresses
only what differs between arms — which is the whole point of config-as-data.
`instrument` is filled from `symbol_maps` when absent, so a run cannot silently
size against a made-up value_per_point.

GENERATED SIGNALS (#184)
------------------------
Set `signal_source` to `generator:rules` and the signals are COMPUTED from the
bars instead of read from `signals` — the answer to "what if the entry came from
an indicator instead of a Telegram channel?". Everything downstream is unchanged:
the generator emits the same `ParsedSignal`, so planner, sizing, staging,
`sl_rules`, risk caps, breaker, sessions and metrics are byte-identical.

    "signal_source": "generator:rules",
    "generator_config": {
      "timeframe": "15m",
      "long":  {"when": {"all": [
        {"type": "indicator", "id": "macd", "timeframe": "15m",
         "field": "cross", "op": "eq", "value": "up"},   # "up"/"down", not "bull"
        {"type": "indicator", "id": "rsi", "timeframe": "15m",
         "field": "value", "op": "lt", "value": 70}]}},
      "entry": {"type": "close"},
      "sl":    {"type": "atr_mult", "timeframe": "1h", "period": 14, "mult": 1.5},
      "tps":   [{"type": "r_mult", "r": 1}, {"type": "r_mult", "r": 2}],
      "cooldown_bars": 60,
      "max_signals_per_day": 8
    }

A new strategy is JSON, not a deploy — the condition grammar is the SAME one
`entry_filters` uses (`execution/strategy.py`), so any registry indicator is
expressible without touching this code. `check --config` resolves it offline and
prints the indicator instances it will compute.

Generated signals carry NEGATIVE `signal_id`s in `replay_results`, so a join
against the trading `signals` table cannot silently match the wrong rows.

**THIS IS A SCREENING STEP, NOT A ROUTE TO LIVE.** A validated generator still
needs the Lever-5 chain — a `kind='engine'` source (which does not exist today),
a producer, and shadow forward-R — before it can trade. And it is not actionable
at all until #169's validation gate has passed: fitting a strategy to a simulator
nobody has verified compounds two unknowns.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

from sqlalchemy import text

from beacon_core.execution import strategy as ST

from harness import bars as B
from harness import runner as R
from harness import scaffold, store, validate
from harness import signal_sources as SS
from harness.models import REPLAY_SCHEMA
from harness.context import ContextBuilder
from harness.portfolio import PortfolioSim, SignalRow
from harness.variants import RATCHET_TIMINGS, build_variant
# Imported for its side effect: importing the module is what REGISTERS
# `generator:rules` (#184). Nothing else here references it by name.
from harness import generators as _generators            # noqa: F401

# `main.py` is the ONLY module that touches the database, and it imports nothing
# that can place an order. `tests/test_isolation.py` asserts both, statically and
# at import time, so the guarantee is enforced rather than described.


def _dt(v):
    if not v:
        return None
    s = str(v).replace("Z", "+00:00")
    d = dt.datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _merged_variants(cfg: dict, symbol_map: dict | None) -> list:
    """`defaults` under each variant, with `instrument` filled from the symbol map
    when the config did not state one."""
    defaults = cfg.get("defaults") or {}
    out = []
    for v in (cfg.get("variants") or []):
        merged = {**defaults, **v}
        if "instrument" not in merged and symbol_map:
            merged["instrument"] = dict(symbol_map)
        out.append(merged)
    return out


def _spec(cfg: dict, variants: list) -> R.RunSpec:
    return R.RunSpec(
        label=str(cfg.get("label") or ""),
        symbol=str(cfg.get("symbol") or "XAUUSD"),
        timeframe=str(cfg.get("timeframe") or "1m"),
        frm=_dt(cfg.get("from")), to=_dt(cfg.get("to")),
        holdout_from=_dt(cfg.get("holdout_from")),
        signal_source=str(cfg.get("signal_source") or "historical"),
        generator_config=cfg.get("generator_config") or {},
        variants=variants, workers=int(cfg.get("workers") or 0),
        source_ids=cfg.get("source_ids"), account_ids=cfg.get("account_ids"))


# Generated signals carry NEGATIVE ids. `replay_results.signal_id` is a plain
# integer with no FK, so a positive one would be indistinguishable from a real
# `signals.id` when someone joins a generated run against the trading tables —
# and that join would silently succeed on the wrong rows.
def _generated_rows(generated, *, account_ids, frm=None, to=None) -> list:
    """`GeneratedSignal`s -> `SignalRow`s, windowed to the run's date range.

    The window is applied HERE rather than inside the generator: bars are loaded
    wider than the run window on purpose (an indicator needs history), so the
    generator legitimately scans outside it and only the EMISSIONS are clipped."""
    rows = []
    for i, g in enumerate(generated):
        if frm is not None and g.at < frm:
            continue
        if to is not None and g.at >= to:
            continue
        rows.append(SignalRow(id=-(i + 1), at=g.at, parsed=g.parsed,
                              source_id=None, source_name="generator",
                              account_ids=tuple(account_ids)))
    return rows


def _generator_account_ids(cfg: dict, spec: R.RunSpec) -> tuple:
    """Which accounts a generated signal routes to.

    A generated signal has no `source`, so there is no `account_map` to read.
    Explicit `account_ids` wins; otherwise every account the variants declare —
    which is what "run this strategy on the book as configured" means."""
    if spec.account_ids:
        return tuple(spec.account_ids)
    ids = []
    for v in (spec.variants or []):
        for a in (v.get("accounts") or []):
            aid = a.get("id")
            if aid is not None and aid not in ids:
                ids.append(aid)
    return tuple(ids)


# Exit ladders the portal can ask for by NAME. The browser must not be able to
# author sl_rules JSON: a malformed ladder there produces a run that completes,
# reports rows, and takes zero trades — which is what the first portal-launched
# sweep did (1,873 signals evaluated, `unknown_account` on 1,761 of them).
PORTAL_LADDERS = {
    "be_at_tp1": [
        {"trigger": {"type": "tp_hit", "index": 1},
         "action": {"type": "move_sl_to", "target": "entry"}},
        {"trigger": {"type": "tp_hit", "index": 2},
         "action": {"type": "move_sl_to", "target": "previous_tp"}}],
    "be_at_tp2": [
        {"trigger": {"type": "tp_hit", "index": 2},
         "action": {"type": "move_sl_to", "target": "entry"}},
        {"trigger": {"type": "tp_hit", "index": 3},
         "action": {"type": "move_sl_to", "target": "previous_tp"}}],
    # An EMPTY sl_rules list reads as UNSET and cascades to the default ladder,
    # so a true control has to be a rule that can never fire.
    "runner_no_ratchet": [
        {"trigger": {"type": "tp_hit", "index": 99},
         "action": {"type": "move_sl_to", "target": "entry"}}],
}
DEFAULT_PORTAL_EQUITY = 10000.0


async def _expand_scaffold(session, cfg: dict) -> dict:
    """Turn a portal request into a full run config, from the LIVE tables.

    A queued job carries a high-level ask — window, holdout, which exit ladders —
    and the accounts, risk, risk_limits, instrument and session windows are read
    from the database here, exactly as `scaffold` does for the validation
    baseline. The browser never authors them.

    That is not tidiness. The first portal-launched sweep sent variants with a
    name and nothing else, so every account lookup missed and all 5,619 rows were
    `not_taken`. The run said `done`. Building the config where the live config
    actually lives makes that unrepresentable, and keeps a portal sweep in step
    with production instead of with whatever the page last hardcoded."""
    if not cfg.get("scaffold"):
        return cfg
    names = [n for n in (cfg.get("ladders") or []) if n in PORTAL_LADDERS]
    if not names:
        raise ValueError("scaffold run needs at least one known ladder: "
                         + ", ".join(sorted(PORTAL_LADDERS)))
    symbol = str(cfg.get("symbol") or "XAUUSD")
    equity = float(cfg.get("equity") or DEFAULT_PORTAL_EQUITY)
    base = await store.load_live_config(
        session, symbol=symbol, equity=equity, frm=_dt(cfg.get("from")),
        to=_dt(cfg.get("to")), holdout_from=_dt(cfg.get("holdout_from")))
    live = (base.get("variants") or [{}])[0]

    variants = []
    for name in names:
        import copy
        v = copy.deepcopy(live)
        v["name"] = name
        for st in v.get("strategies", []):
            ep = st.setdefault("exit_policy", {})
            if st.get("account_id") is None and st.get("source_id") is None:
                ep["sl_rules"] = copy.deepcopy(PORTAL_LADDERS[name])
                st["label"] = name
            else:
                # Let the base layer win uniformly, or the arms would differ by
                # a mix of two changes and neither would be attributable.
                ep.pop("sl_rules", None)
        variants.append(v)
    return {**base, "label": cfg.get("label") or base.get("label"),
            "from": cfg.get("from"), "to": cfg.get("to"),
            "holdout_from": cfg.get("holdout_from"),
            "signal_source": "historical", "workers": 2, "variants": variants}


async def _load(session, cfg: dict):
    cfg = await _expand_scaffold(session, cfg)
    symbol = str(cfg.get("symbol") or "XAUUSD")
    timeframe = str(cfg.get("timeframe") or "1m")
    frm, to = _dt(cfg.get("from")), _dt(cfg.get("to"))
    smap = await store.load_symbol_map(session, symbol)
    variants = _merged_variants(cfg, smap)
    spec = _spec(cfg, variants)
    # Bars are read WIDER than the signal window: an indicator needs history
    # before the first signal, and a trade opened on the last day needs bars
    # after it to resolve. A run that clipped both ends would silently label
    # every late trade horizon-capped.
    series = await store.load_series(session, symbol=symbol, timeframe=timeframe)
    gen_stats: dict = {}
    if SS.is_generator(spec.signal_source):
        # Phase 2 (#184): the signals are COMPUTED from the bars, not read from
        # `signals`. Everything downstream is byte-identical — the rows are the
        # same `SignalRow`/`ParsedSignal` the historical source produces.
        generated = SS.run_generator(spec.signal_source, series.bars,
                                     {"symbol": symbol, **(spec.generator_config or {})})
        gen_stats = SS.output_stats(generated)
        signals = _generated_rows(generated,
                                  account_ids=_generator_account_ids(cfg, spec),
                                  frm=frm, to=to)
        gen_stats["n_in_window"] = len(signals)
    else:
        signals = await store.load_signals(session, symbol=symbol, frm=frm, to=to,
                                           source_ids=spec.source_ids,
                                           account_ids=spec.account_ids)
    sources = await store.load_sources(session)
    return spec, series, signals, sources, symbol, timeframe, frm, to, gen_stats


async def cmd_init(args) -> int:
    """Create `replay.replay_runs` / `replay.replay_results`, and say what is
    there afterwards.

    Its real job is to be the CHEAP way to prove the grant works. Everything else
    the harness does to the database is a SELECT; this is the only write, so
    without a standalone command the first proof that the role can create its
    tables would be a sweep that has already burned an hour."""
    await store.init_replay_tables()
    async with store.Session()() as session:
        rows = (await session.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = :s ORDER BY table_name"
        ), {"s": REPLAY_SCHEMA})).scalars().all()
    print(json.dumps({"ok": True, "schema": REPLAY_SCHEMA,
                      "tables": list(rows),
                      "note": "The role can create in its own schema and write "
                              "nowhere else. Safe to run again — create_all "
                              "skips what already exists."}, indent=2))
    return 0


async def cmd_scaffold(args) -> int:
    """Write a run config that reproduces the LIVE setup — the §5 baseline.

    Hand-transcribing `execution_strategies`, `account_source_risk`, the
    `risk_limits` setting and the symbol map into JSON is exactly the work that
    produces a config which is *nearly* live, and a gate run against a
    nearly-live config measures the transcription rather than the simulator."""
    equity = json.loads(args.equity) if args.equity.strip().startswith("{") \
        else float(args.equity)
    async with store.Session()() as session:
        cfg = await store.load_live_config(
            session, symbol=args.symbol, equity=equity, frm=args.since,
            to=args.to, holdout_from=args.holdout_from)
    out = Path(args.out)
    if out.exists() and not args.force:
        print(json.dumps({"ok": False, "error": f"{out} exists; pass --force to "
                                                "overwrite"}, indent=2))
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cfg, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "written": str(out),
                      **scaffold.summarise(cfg)}, indent=2, default=str))
    return 0


def _warn_stale_candles(series, to=None) -> None:
    """Say so when the bars end well before now (#190).

    The candle store is a manual import and nothing in the repo refreshes it. A
    sweep over a stale store does not fail — it marks late trades
    `horizon_capped`, which reads as a patient variant rather than as missing
    data. Loud beats subtly wrong."""
    import datetime as _dt
    last = series.last_ts
    if last is None:
        return
    age_h = (_dt.datetime.now(_dt.timezone.utc) - last).total_seconds() / 3600.0
    if age_h <= 24:
        return
    print(f"WARNING: candle store ends {last.isoformat()} "
          f"({age_h:.1f}h ago). Trades after that cannot resolve and will be "
          f"reported horizon-capped, which is missing data rather than a "
          f"patient variant. See #190 — candles are imported manually and "
          f"nothing refreshes them.", file=sys.stderr)


def _warn_unattributable() -> None:
    """Say so, on stderr, when a run cannot name the code that produced it.

    `git_sha` was NULL on every run ever stored here and nothing said a word:
    the image has no `.git` and no `git` binary, so the lookup failed silently
    and #169's reproducibility column recorded nothing. A missing sha also stops
    a sweep inheriting a gate verdict, because there is no code identity to match
    on. Loud is the only honest setting for a guarantee that has quietly not
    been holding."""
    if R.git_sha():
        return
    print("WARNING: git_sha is unknown, so this run cannot be attributed to a "
          "commit and no sweep can inherit its gate verdict. Rebuild with:\n"
          "  GIT_SHA=$(git rev-parse HEAD) docker compose build replay",
          file=sys.stderr)


async def cmd_run(args) -> int:
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    # BEFORE the sweep, not after. A missing grant or a missing schema is a
    # two-second failure; discovering it once 800 signals x N variants have
    # already been simulated costs an afternoon and teaches nothing.
    if not args.dry_run:
        await store.init_replay_tables()
    async with store.Session()() as session:
        (spec, series, signals, sources, symbol, tf, frm, to,
         gen_stats) = await _load(session, cfg)
        if not len(series):
            print(json.dumps({"ok": False,
                              "error": f"no usable candles for {symbol} {tf}"}, indent=2))
            return 2
        out = R.sweep(spec, series, signals, sources_by_id=sources,
                      generator_stats=gen_stats)
        reports = out["variants"]
        summary = {"run": out["run"], "variants": reports,
                   "ranking": R.compare_variants(reports)}
        if args.dry_run:
            print(json.dumps(summary, indent=2, default=str))
            return 0

        cdig = await store.candle_digest(session, symbol=symbol, timeframe=tf)
        _warn_unattributable()
        _warn_stale_candles(series, to)
        run = await store.create_run(
            session, label=spec.label, signal_source=spec.signal_source,
            symbol=symbol, timeframe=tf, frm=frm, to=to,
            holdout_from=spec.holdout_from, n_variants=len(spec.variants),
            git_sha=R.git_sha(), code_version=R.CODE_VERSION,
            config_digest=spec.digest(), candle_digest=cdig, config=cfg,
            coverage=series.coverage())
        rows = []
        for name, res in sorted(out["results"].items()):
            rows.extend(store.result_rows(run.id, name, res,
                                          holdout_from=spec.holdout_from))
        await store.write_results(session, rows)
        await store.finish_run(session, run, summary=summary,
                               coverage=series.coverage())
        print(json.dumps({"ok": True, "run_id": run.id, "rows": len(rows),
                          "ranking": summary["ranking"]}, indent=2, default=str))
    return 0


async def _run_whatif(session, job, cfg: dict) -> tuple:
    """Run the SAME signals twice — as they were, and with one change (#183).

    The engine is the shipped one; only the framing is new. Both arms see an
    identical signal set, so any difference in the result is the change and
    nothing else."""
    from harness import whatif as W

    scope = cfg.get("scope") or {}
    symbol = str(cfg.get("symbol") or "XAUUSD")
    equity = float(cfg.get("equity") or DEFAULT_PORTAL_EQUITY)
    frm, to = _dt(cfg.get("from")), _dt(cfg.get("to"))

    base_cfg = await store.load_live_config(
        session, symbol=symbol, equity=equity, frm=frm, to=to)
    live = (base_cfg.get("variants") or [{}])[0]
    changes = cfg.get("changes") or {}

    # SCOPE. Source-wise keeps one channel; account-wise keeps one account and
    # every channel that reaches it; otherwise the whole book.
    source_ids = [int(scope["source_id"])] if (
        scope.get("type") == "source" and scope.get("source_id")) else None
    account_ids = [int(scope["account_id"])] if (
        scope.get("type") == "account" and scope.get("account_id")) else None

    series = await store.load_series(session, symbol=symbol, timeframe="1m")
    if not len(series):
        raise ValueError(f"no usable candles for {symbol}")
    signals = await store.load_signals(session, symbol=symbol, frm=frm, to=to,
                                       source_ids=source_ids,
                                       account_ids=account_ids,
                                       strict_accounts=bool(account_ids))
    sources = await store.load_sources(session)
    scope_label = (sources.get(source_ids[0], f"source #{source_ids[0]}")
                   if source_ids else
                   (f"account #{account_ids[0]}" if account_ids else "all sources"))
    await store.set_job_progress(
        session, job, f"{len(signals)} signals · {scope_label}")

    ctx = ContextBuilder(series)
    base_v = build_variant({**live, "name": "baseline"})
    alt_v = build_variant({**W.apply_changes(live, changes), "name": "whatif"})
    base_res = PortfolioSim(base_v, series, ctx=ctx).run(signals)

    # GEOMETRY FILTERS run here, not in the engine: the live filtration grammar
    # has no `when.type` for stop distance, and #189 measured that sub-ATR stops
    # were the largest single R leak on the control arm — so it is the one
    # what-if worth asking that the engine cannot express. Applied by removing
    # the signal from the alt arm and re-declaring it as a skip, so both columns
    # still total the same signal count; dropping it silently would make the
    # what-if arm look like it saw fewer signals rather than rejecting them.
    geo = [f for f in (changes.get("filters") or [])
           if f.get("kind") in W.GEOMETRY_KINDS]
    alt_signals, geo_skipped = signals, []
    if geo:
        alt_signals = []
        for s in signals:
            atr = ctx.atr(W.ATR_TIMEFRAME, s.at)
            hit = next((f for f in geo if W.geometry_skip(f, s, atr)), None)
            (geo_skipped if hit else alt_signals).append(
                {"signal_id": s.id, "reason": "whatif:" + str(hit.get("kind"))}
                if hit else s)

    alt_res = PortfolioSim(alt_v, series, ctx=ctx).run(alt_signals)
    alt_res.not_taken.extend(geo_skipped)

    rep = W.report(base_res, alt_res, changes=changes, scope_label=scope_label,
                   frm=frm, to=to, signals=signals)
    return rep, {"baseline": base_res, "whatif": alt_res}, series, symbol, frm, to


async def cmd_worker(args) -> int:
    """Drain the job queue the portal appends to (#183).

    THE ONE LONG-RUNNING THING IN THIS SERVICE, and it stays inside the same
    isolation: no broker credentials, a SELECT-only role on the trading schema,
    write access only to `replay.*`, `nice -19` from the image ENTRYPOINT and a
    hard CPU/memory ceiling from compose. What it adds is that a sweep can be
    requested from a browser instead of an SSH session; what it does not add is
    a sweep running inside the API, or more than one running at a time.

    ONE JOB AT A TIME, deliberately. A portal full of impatient clicks becomes a
    queue rather than N concurrent 400k-bar sweeps competing with `monitor` for
    the box that manages open positions. That was the whole objection to a
    browser-startable replay, and serialising is what answers it.

    A failed job fails ITS OWN row and the loop continues — one bad config must
    not stop the queue — and a job left RUNNING by a killed worker is marked
    failed rather than silently retried, since a partial sweep may already have
    written a run."""
    await store.init_replay_tables()
    log = print
    log(json.dumps({"worker": "started", "poll_seconds": args.poll,
                    "git_sha": R.git_sha()}, default=str))
    while True:
        try:
            async with store.Session()() as session:
                n = await store.requeue_stale_running_jobs(session)
                if n:
                    log(json.dumps({"worker": "reaped_stale_jobs", "n": n}))
                job = await store.claim_next_job(session)
            if job is None:
                await asyncio.sleep(args.poll)
                continue
            log(json.dumps({"worker": "claimed", "job": job.id,
                            "label": job.label}, default=str))
            # Pass the ID, never the instance. The session that claimed it has
            # closed, and a detached ORM object accepts attribute writes that a
            # DIFFERENT session's commit silently discards — which is exactly
            # how the first queued job wrote all 3,866 of its result rows and
            # then sat at `running` forever with 0% CPU.
            rc = await _execute_job(job.id)
            log(json.dumps({"worker": "finished", "job": job.id, "result": rc},
                           default=str))
        except asyncio.CancelledError:
            raise
        except Exception as exc:                 # the loop never dies on one job
            log(json.dumps({"worker": "loop_error", "error": str(exc)[:300]}))
            await asyncio.sleep(args.poll)
        if args.once:
            return 0


async def _execute_job(job_id: int) -> str:
    """Run one queued job to completion and record what happened to it.

    Takes the ID and re-loads the row inside THIS session: every write below has
    to happen through a session the object is actually attached to, or it is
    discarded without error."""
    from harness.models import ReplayJob
    try:
        async with store.Session()() as session:
            job = await session.get(ReplayJob, job_id)
            if job is None:
                return "vanished"
            cfg = dict(job.config or {})
            await store.set_job_progress(session, job, "loading candles and signals")
            if cfg.get("mode") == "whatif":
                rep, results, series, symbol, frm, to = await _run_whatif(
                    session, job, cfg)
                cdig = await store.candle_digest(session, symbol=symbol,
                                                 timeframe="1m")
                run = await store.create_run(
                    session, label=(cfg.get("label") or job.label or "what-if"),
                    signal_source="historical", symbol=symbol, timeframe="1m",
                    frm=frm, to=to, n_variants=2, git_sha=R.git_sha(),
                    code_version=R.CODE_VERSION, config_digest="",
                    candle_digest=cdig, config=cfg, coverage=series.coverage())
                rows = []
                for name, res in sorted(results.items()):
                    rows.extend(store.result_rows(run.id, name, res))
                await store.write_results(session, rows)
                await store.finish_run(session, run, summary={"whatif": rep},
                                       coverage=series.coverage())
                await store.finish_job(session, job, status="done", run_id=run.id,
                                       progress=rep["verdict"]["headline"][:160])
                return "done"
            (spec, series, signals, sources, symbol, tf, frm, to,
             gen_stats) = await _load(session, cfg)
            if not len(series):
                await store.finish_job(session, job, status="failed",
                                       error=f"no usable candles for {symbol} {tf}")
                return "failed"
            await store.set_job_progress(
                session, job,
                f"{len(spec.variants)} variant(s) over {len(signals)} signals")
            out = R.sweep(spec, series, signals, sources_by_id=sources,
                          generator_stats=gen_stats)
            reports = out["variants"]
            summary = {"run": out["run"], "variants": reports,
                       "ranking": R.compare_variants(reports)}
            cdig = await store.candle_digest(session, symbol=symbol, timeframe=tf)
            _warn_unattributable()
            _warn_stale_candles(series, to)
            run = await store.create_run(
                session, label=spec.label or job.label, signal_source=spec.signal_source,
                symbol=symbol, timeframe=tf, frm=frm, to=to,
                holdout_from=spec.holdout_from, n_variants=len(spec.variants),
                git_sha=R.git_sha(), code_version=R.CODE_VERSION,
                config_digest=spec.digest(), candle_digest=cdig, config=cfg,
                coverage=series.coverage())
            rows = []
            for name, res in sorted(out["results"].items()):
                rows.extend(store.result_rows(run.id, name, res,
                                              holdout_from=spec.holdout_from))
            await store.write_results(session, rows)
            await store.finish_run(session, run, summary=summary,
                                   coverage=series.coverage())
            await store.finish_job(session, job, status="done", run_id=run.id,
                                   progress=f"{len(rows)} rows")
            return "done"
    except Exception as exc:
        async with store.Session()() as session:
            fresh = await session.get(ReplayJob, job_id)
            if fresh is not None:
                await store.finish_job(session, fresh, status="failed",
                                       error=str(exc)[:2000])
        return "failed"


async def cmd_validate(args) -> int:
    """Section 5: replay the ACTUAL live configs over the ACTUAL signals and
    reconcile against broker truth. Exits NON-ZERO when the gate fails — a
    failed gate is a blocking defect, and a validation step that always exits 0
    is decoration.

    The verdict is STORED as a run (unless `--dry-run`), so a sweep can inherit
    it instead of the portal claiming an unvalidated simulator."""
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    # BEFORE the sweep, exactly as `run` does: a missing grant is a two-second
    # failure, and finding out after a full reconciliation costs an afternoon.
    if not args.dry_run:
        await store.init_replay_tables()
    # #185 defect B: the exit model is a modelling CHOICE with a measured cost,
    # so it has to be settled by running this gate both ways. An override beats
    # editing the baseline JSON between runs — the baseline is generated from the
    # live tables precisely so nobody hand-edits it.
    # Applied to the VARIANTS, not only to `defaults`: `defaults` is merged UNDER
    # each variant, so a baseline that already stated a timing would silently win
    # and the operator would compare a run against itself.
    if getattr(args, "ratchet_timing", None):
        cfg = {**cfg,
               "defaults": {**(cfg.get("defaults") or {}),
                            "ratchet_timing": args.ratchet_timing},
               "variants": [{**v, "ratchet_timing": args.ratchet_timing}
                            for v in (cfg.get("variants") or [])]}
    async with store.Session()() as session:
        (spec, series, signals, sources, symbol, tf, frm, to,
         _gen) = await _load(session, cfg)
        if not len(series):
            print(json.dumps({"ok": False, "error": "no usable candles"}, indent=2))
            return 2
        out = R.sweep(spec, series, signals, sources_by_id=sources)
        truth = await store.load_live_truth(session, symbol=symbol, frm=frm, to=to)
        report = {}
        failed = False
        for name, res in sorted(out["results"].items()):
            sim_legs, sim_trades = store.sim_legs_for_validation(res)
            report[name] = rep = validate.report(
                sim_legs, truth["legs"], sim_trades, truth["trades"])
            failed = failed or not rep["gate"]["passed"]
        # The exit model is stated on the OUTPUT: two gate runs that differ on it
        # produce different numbers, and a comparison whose arms are not labelled
        # is not a comparison (#185).
        timings = sorted({build_variant(v).ratchet_timing
                          for v in (spec.variants or [])}) or ["next_bar"]

        # PERSIST the verdict. A gate run used to print and vanish, which meant
        # the portal showed "not validated" on every sweep even when the
        # simulator HAD been reconciled against broker truth minutes earlier —
        # the verdict existed only in somebody's terminal scrollback. Stored, it
        # is a first-class run: reproducible (git_sha / config_digest /
        # candle_digest), readable over the API, and inheritable by any sweep on
        # the same code and the same bars (see the router's `_validation_index`).
        stored_run_id = None
        if not args.dry_run:
            cdig = await store.candle_digest(session, symbol=symbol, timeframe=tf)
            _warn_unattributable()
            _warn_stale_candles(series, to)
            run = await store.create_run(
                session, label=(spec.label or "validation gate"),
                signal_source=spec.signal_source, symbol=symbol, timeframe=tf,
                frm=frm, to=to, holdout_from=spec.holdout_from,
                n_variants=len(spec.variants), git_sha=R.git_sha(),
                code_version=R.CODE_VERSION, config_digest=spec.digest(),
                candle_digest=cdig, config=cfg, coverage=series.coverage())
            await store.finish_run(
                session, run,
                summary={"run": out["run"], "variants": out["variants"],
                         "ranking": R.compare_variants(out["variants"])},
                coverage=series.coverage(),
                validation=validate.overall(report),
                status="done" if not failed else "failed",
                error=None if not failed else "validation gate failed")
            stored_run_id = run.id

        print(json.dumps({"ok": not failed, "coverage": series.coverage(),
                          "ratchet_timing": timings,
                          "run_id": stored_run_id,
                          "validation": report}, indent=2, default=str))
    return 1 if failed else 0


async def cmd_coverage(args) -> int:
    """The checks #169's comment lists as 'still to verify before trusting any
    replay output'. Run this FIRST: if the candles do not span the live signal
    window, the validation gate has nothing to validate against."""
    async with store.Session()() as session:
        series = await store.load_series(session, symbol=args.symbol,
                                         timeframe=args.timeframe)
        frm = _dt(args.since)
        over = sum(1 for b in series.bars if frm is None or b.ts >= frm)
        signals = await store.load_signals(session, symbol=args.symbol, frm=frm)
        last_bar = series.last_ts
        # Signals the candle store does not reach are EXCLUSIONS, and they are
        # counted here rather than discovered as `no_candle_coverage` rows after
        # a run. The tail matters more than it looks: the feed lags live, so the
        # newest signals — the ones an operator is most curious about — are
        # exactly the ones most likely to be uncovered.
        uncovered = [s for s in signals if last_bar and s.at > last_bar]
        print(json.dumps({
            "candles": series.coverage(),
            "bars_over_live_window": over,
            "since": args.since,
            "n_signals_in_window": len(signals),
            "first_signal": min((s.at for s in signals), default=None),
            "last_signal": max((s.at for s in signals), default=None),
            "n_signals_after_last_candle": len(uncovered),
            "n_signals_evaluable": len(signals) - len(uncovered),
            "candle_lag_behind_last_signal": (
                str(max((s.at for s in signals), default=last_bar) - last_bar)
                if last_bar and signals else None),
            "gaps": B.daily_gaps(series.bars, frm=frm),
            "note": ("bars_over_live_window == 0 means the validation gate has "
                     "nothing to validate against. suspect_excluded is the "
                     "crossed-quote/outlier count that every result must carry "
                     "as a caveat. Signals after the last candle are EXCLUDED, "
                     "not scored — bound the run with `to` (or scaffold --to) to "
                     "keep them out of the comparison rather than counting as "
                     "trades the harness failed to reproduce."),
        }, indent=2, default=str))
    return 0


def cmd_check(args) -> int:
    """Parse + resolve a run config without touching the database. Catches a
    malformed variant before a sweep burns an afternoon on it.

    A `generator:` run is checked here too (#184): the condition is walked for
    the indicator instances it needs, so an unknown id or a timeframe the
    registry does not carry surfaces as a named requirement gap rather than as a
    strategy that mysteriously never triggers."""
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    variants = _merged_variants(cfg, None)
    spec = _spec(cfg, variants)
    out = {
        "ok": True, "n_variants": len(variants), "config_digest": spec.digest(),
        "signal_source": spec.signal_source,
        "variants": [{"name": build_variant(v).name,
                      "digest": build_variant(v).digest()} for v in variants],
    }
    if SS.is_generator(spec.signal_source):
        try:
            gspec = _generators.RulesSpec(spec.generator_config or {})
        except _generators.ConfigError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            return 2
        reqs = []
        for c in (gspec.long, gspec.short):
            reqs.extend(ST.condition_requirements(c, gspec.timeframe))
        out["generator"] = {
            "name": SS.generator_name(spec.signal_source),
            "timeframe": gspec.timeframe,
            "cooldown_bars": gspec.cooldown_bars,
            "max_signals_per_day": gspec.max_per_day,
            "indicator_instances": sorted(
                {f"{r['timeframe']}:{r['key']}" for r in reqs}),
            "accounts": list(_generator_account_ids(cfg, spec)),
            "note": ("An indicator the condition names but the registry does not "
                     "carry is absent from `indicator_instances` — that condition "
                     "is UNKNOWN on every bar and the generator will emit nothing "
                     "(fail-open), which is silent unless you look here."),
        }
    print(json.dumps(out, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="replay", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the replay_* tables and prove the grant "
                                "works (cheap; run this first)")

    s = sub.add_parser("scaffold", help="write a run config reproducing the LIVE "
                                        "setup — the validation baseline")
    s.add_argument("--out", default="runs/live-config.json")
    s.add_argument("--equity", required=True,
                   help='account equity: one number for all accounts, or a JSON '
                        'map \'{"1": 10000, "2": 10000}\'. Read from the broker, '
                        'not the ledger — sizing is only comparable to live if '
                        'the budget is.')
    s.add_argument("--symbol", default="XAUUSD")
    s.add_argument("--since", default=None, help="signal window start (ISO-8601)")
    s.add_argument("--to", default=None,
                   help="signal window END (ISO-8601). Set it to the LAST CANDLE "
                        "when the feed lags live: signals past the candle store "
                        "cannot be replayed, and leaving them in makes the "
                        "validation gate count them as trades the harness failed "
                        "to reproduce. `coverage` prints the boundary.")
    s.add_argument("--holdout-from", dest="holdout_from", default=None)
    s.add_argument("--force", action="store_true", help="overwrite --out")

    r = sub.add_parser("run", help="sweep N config variants over the signal history")
    r.add_argument("--config", required=True)
    r.add_argument("--dry-run", action="store_true",
                   help="print the report; write nothing")

    v = sub.add_parser("validate", help="reconcile a replay of the LIVE config "
                                        "against broker truth (§5 gate)")
    v.add_argument("--config", required=True)
    v.add_argument("--dry-run", action="store_true",
                   help="print the verdict; store nothing. The gate normally "
                        "persists its result as a run so sweeps can inherit it.")
    v.add_argument("--ratchet-timing", dest="ratchet_timing", default=None,
                   choices=list(RATCHET_TIMINGS),
                   help="override the exit model for this run (#185 defect B). "
                        "next_bar (default) applies a ratchet from the FOLLOWING "
                        "bar; same_bar re-tests the moved stop against this "
                        "bar's adverse extreme. Run the gate BOTH ways and keep "
                        "whichever brings mean delta R closer to zero while "
                        "agreement holds — then document the loser's residual.")

    c = sub.add_parser("coverage", help="candle coverage over the live signal window")
    c.add_argument("--symbol", default="XAUUSD")
    c.add_argument("--timeframe", default="1m")
    c.add_argument("--since", default="2026-07-05T00:00:00Z")

    w = sub.add_parser("worker", help="drain the portal's replay job queue "
                                      "(the only long-running command)")
    w.add_argument("--poll", type=float, default=5.0,
                   help="seconds between queue polls when idle")
    w.add_argument("--once", action="store_true",
                   help="handle at most one job, then exit (for testing)")

    k = sub.add_parser("check", help="validate a run config offline (no DB)")
    k.add_argument("--config", required=True)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "check":
        return cmd_check(args)
    fn = {"init": cmd_init, "scaffold": cmd_scaffold, "run": cmd_run,
          "validate": cmd_validate, "coverage": cmd_coverage,
          "worker": cmd_worker}[args.cmd]
    return asyncio.run(fn(args))


if __name__ == "__main__":
    sys.exit(main())
