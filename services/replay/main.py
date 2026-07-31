"""services/replay — the offline signal-replay + backtest harness (#169).

A BATCH JOB, not a daemon. There is no loop, no queue consumer and no HTTP
server, because the safest way to be incapable of disturbing live trading is to
not be running. It is invoked explicitly:

    docker compose run --rm replay python main.py run --config runs/exit.json
    docker compose run --rm replay python main.py validate --config runs/live.json
    docker compose run --rm replay python main.py coverage

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
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

from harness import runner as R
from harness import store, validate
from harness.variants import build_variant

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


async def _load(session, cfg: dict):
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
    signals = await store.load_signals(session, symbol=symbol, frm=frm, to=to,
                                       source_ids=spec.source_ids,
                                       account_ids=spec.account_ids)
    sources = await store.load_sources(session)
    return spec, series, signals, sources, symbol, timeframe, frm, to


async def cmd_run(args) -> int:
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    async with store.Session()() as session:
        spec, series, signals, sources, symbol, tf, frm, to = await _load(session, cfg)
        if not len(series):
            print(json.dumps({"ok": False,
                              "error": f"no usable candles for {symbol} {tf}"}, indent=2))
            return 2
        out = R.sweep(spec, series, signals, sources_by_id=sources)
        reports = out["variants"]
        summary = {"run": out["run"], "variants": reports,
                   "ranking": R.compare_variants(reports)}
        if args.dry_run:
            print(json.dumps(summary, indent=2, default=str))
            return 0

        await store.init_replay_tables()
        cdig = await store.candle_digest(session, symbol=symbol, timeframe=tf)
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


async def cmd_validate(args) -> int:
    """Section 5: replay the ACTUAL live configs over the ACTUAL signals and
    reconcile against broker truth. Exits NON-ZERO when the gate fails — a
    failed gate is a blocking defect, and a validation step that always exits 0
    is decoration."""
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    async with store.Session()() as session:
        spec, series, signals, sources, symbol, tf, frm, to = await _load(session, cfg)
        if not len(series):
            print(json.dumps({"ok": False, "error": "no usable candles"}, indent=2))
            return 2
        out = R.sweep(spec, series, signals, sources_by_id=sources)
        truth = await store.load_live_truth(session, symbol=symbol, frm=frm, to=to)
        report = {}
        failed = False
        for name, res in sorted(out["results"].items()):
            sim_legs, sim_trades = store.sim_legs_for_validation(res)
            rep = validate.report(sim_legs, truth["legs"], sim_trades, truth["trades"])
            rep.pop("legs", {}).pop("rows", None)     # keep the summary readable
            report[name] = rep
            failed = failed or not rep["gate"]["passed"]
        print(json.dumps({"ok": not failed, "coverage": series.coverage(),
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
        print(json.dumps({
            "candles": series.coverage(),
            "bars_over_live_window": over,
            "since": args.since,
            "n_signals_in_window": len(signals),
            "first_signal": min((s.at for s in signals), default=None),
            "last_signal": max((s.at for s in signals), default=None),
            "note": ("bars_over_live_window == 0 means the validation gate has "
                     "nothing to validate against. suspect_excluded is the "
                     "crossed-quote/outlier count that every result must carry "
                     "as a caveat."),
        }, indent=2, default=str))
    return 0


def cmd_check(args) -> int:
    """Parse + resolve a run config without touching the database. Catches a
    malformed variant before a sweep burns an afternoon on it."""
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    variants = _merged_variants(cfg, None)
    spec = _spec(cfg, variants)
    print(json.dumps({
        "ok": True, "n_variants": len(variants), "config_digest": spec.digest(),
        "variants": [{"name": build_variant(v).name,
                      "digest": build_variant(v).digest()} for v in variants],
    }, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="replay", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="sweep N config variants over the signal history")
    r.add_argument("--config", required=True)
    r.add_argument("--dry-run", action="store_true",
                   help="print the report; write nothing")

    v = sub.add_parser("validate", help="reconcile a replay of the LIVE config "
                                        "against broker truth (§5 gate)")
    v.add_argument("--config", required=True)

    c = sub.add_parser("coverage", help="candle coverage over the live signal window")
    c.add_argument("--symbol", default="XAUUSD")
    c.add_argument("--timeframe", default="1m")
    c.add_argument("--since", default="2026-07-05T00:00:00Z")

    k = sub.add_parser("check", help="validate a run config offline (no DB)")
    k.add_argument("--config", required=True)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "check":
        return cmd_check(args)
    fn = {"run": cmd_run, "validate": cmd_validate, "coverage": cmd_coverage}[args.cmd]
    return asyncio.run(fn(args))


if __name__ == "__main__":
    sys.exit(main())
