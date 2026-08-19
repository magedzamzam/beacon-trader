#!/usr/bin/env python3
"""Run every enabled `engine` source against the newest closed bar (#224).

The last piece of the Lever-5 chain. It loads recent 1m bars from `public.candles`
(kept current by `candle-sync`), builds the same market context the backtest
builds, asks `beacon_core.generator.producer` whether the newest CLOSED bar
produces a signal, and writes a `status='shadow'` row when it does.

  python engine_producer.py [--symbol XAUUSD] [--lookback-days 7]
                            [--loop SECONDS] [--dry-run]

IT CANNOT PLACE AN ORDER. A signal reaches the executor exactly once, by being
enqueued onto the durable queue in `ingest/pipeline.py`. Nothing here imports the
bus or enqueues anything, so a shadow signal is inert by construction rather than
by a flag someone could flip. `enabled_for_trading=false` on the source is the
second, independent guard.

WHAT SCORES THEM is already built: `analysis/excursion_store` computes a
`BASIS_SIGNAL` excursion for every signal from the 1m bars, whether or not it was
ever traded. So writing the row IS the measurement, and after ~6 weeks at this
generator's cadence there is an N worth ruling on.

Idempotent on the bar: a signal is stamped with its bar's CLOSE time, and a bar
that already produced one for that source is skipped. Running this every minute
and running it every fifteen produce the same ledger.
"""
import argparse
import asyncio
import datetime as dt
import os
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from beacon_core.db.models import Candle, Signal, Source
from beacon_core.generator import producer as P
from beacon_core.generator import rules as G
from beacon_core.generator.config import KIND_ENGINE, engine_config
from beacon_core.market import bars as B
from beacon_core.market.context import ContextBuilder

DEFAULT_LOOKBACK_DAYS = 7
# How far back one pass may repair. 16 bars is 4h on a 15m frame:
# enough to cover a deploy, a restart or a slow broker fetch, and far
# too short to replay a week of stale conditions as though they had
# just fired.
DEFAULT_CATCHUP_BARS = 16


async def _load_bars(session, symbol: str, since: dt.datetime):
    """The 1m bid/ask series, quality-filtered.

    `quality='suspect'` (crossed quotes, outlier wicks) is EXCLUDED rather than
    repaired -- there is no way to tell from the row which side is wrong, and a
    generator fed a bad high would emit a signal the market never offered."""
    rows = (await session.execute(
        select(Candle).where(Candle.symbol == symbol,
                             Candle.timeframe == "1m",
                             Candle.quality == "ok",
                             Candle.ts >= since)
        .order_by(Candle.ts))).scalars().all()
    return [B.Bar(ts=c.ts,
                  open_bid=float(c.open_bid), open_ask=float(c.open_ask),
                  high_bid=float(c.high_bid), high_ask=float(c.high_ask),
                  low_bid=float(c.low_bid), low_ask=float(c.low_ask),
                  close_bid=float(c.close_bid), close_ask=float(c.close_ask))
            for c in rows]


async def _ledger_caps(session, source_id: int, closed_at: dt.datetime):
    """`(last_signal_at, count_today)` — the caps, from the only record that
    outlives the process."""
    last = (await session.execute(
        select(Signal.signal_at).where(Signal.source_id == source_id)
        .order_by(Signal.signal_at.desc()).limit(1))).scalar()
    day_start = closed_at.replace(hour=0, minute=0, second=0, microsecond=0)
    today = (await session.execute(
        select(Signal.id).where(Signal.source_id == source_id,
                                Signal.signal_at >= day_start))).all()
    return last, len(today)


async def _already_emitted(session, source_id: int, closed_at: dt.datetime) -> bool:
    """This bar already produced a signal for this source. The guard that makes
    the cadence a scheduling choice rather than a strategy one."""
    hit = (await session.execute(
        select(Signal.id).where(Signal.source_id == source_id,
                                Signal.signal_at == closed_at).limit(1))).scalar()
    return hit is not None


async def run_once(session, symbol: str, lookback_days: int,
                   dry_run: bool = False,
                   catchup_bars: int = DEFAULT_CATCHUP_BARS) -> dict:
    sources = (await session.execute(
        select(Source).where(Source.kind == KIND_ENGINE,
                             Source.archived.is_(False)))).scalars().all()
    out = {"sources": len(sources), "emitted": 0, "reasons": {}}
    if not sources:
        return out

    now = dt.datetime.now(dt.timezone.utc)
    bars = await _load_bars(session, symbol, now - dt.timedelta(days=lookback_days))
    if len(bars) < 100:
        out["reasons"]["no_bars"] = len(bars)
        return out
    builder = ContextBuilder(B.BarSeries(bars))

    for src in sources:
        cfg = engine_config(src.strategy)
        if not cfg:
            out["reasons"]["no_generator_config"] = \
                out["reasons"].get("no_generator_config", 0) + 1
            continue
        try:
            spec = G.RulesSpec({**cfg, "symbol": cfg.get("symbol") or symbol})
        except G.ConfigError as exc:
            # Loudly, not silently: a config that cannot be parsed is a strategy
            # that will never fire, and a flat curve reads as a finding.
            print("source %s: bad generator config: %s" % (src.id, exc), flush=True)
            out["reasons"]["bad_config"] = out["reasons"].get("bad_config", 0) + 1
            continue

        frame = B.resample(bars, spec.timeframe)
        # EVERY closed bar we have not already ruled on, oldest first -- not
        # just the newest (#239). The newest-only read silently dropped any bar
        # the producer was not awake for, and with a 30-minute candle poll
        # against a 15-minute frame that was most of them.
        window = P.closed_bars(frame, now, spec.tf_minutes, limit=catchup_bars)
        # `last_at` is the newest signal for the source, full stop -- the
        # cooldown is a duration and does not reset at midnight. The DAILY cap
        # does, so each day the window touches is seeded from the ledger on its
        # own rather than charging yesterday's emissions against today.
        last_at, _ = await _ledger_caps(session, src.id, now)
        per_day = {}
        for idx, bar, closed_at in window:
            day = closed_at.date()
            if day not in per_day:
                _, per_day[day] = await _ledger_caps(session, src.id, closed_at)
            n_today = per_day[day]
            if await _already_emitted(session, src.id, closed_at):
                out["reasons"]["already_emitted"] = \
                    out["reasons"].get("already_emitted", 0) + 1
                continue
            cond_ctx = G.condition_context(spec, builder, bar.close, closed_at)
            res = P.evaluate_at(spec, builder, frame, idx, closed_at, cond_ctx,
                                last_signal_at=last_at, count_today=n_today)
            if res["signal"] is None:
                key = res["reason"] or "no_trigger"
                out["reasons"][key] = out["reasons"].get(key, 0) + 1
                continue

            row = P.shadow_signal_row(res["signal"], src.id, res["closed_at"])
            print("source %s: SHADOW %s %s @ %s sl=%s tps=%s"
                  % (src.id, row["direction"], row["symbol"], row["signal_at"],
                     row["sl"], row["tps"]), flush=True)
            out["emitted"] += 1
            # Carried forward in-loop rather than re-read: the caps have to see
            # a signal this pass just wrote, or a catch-up over a quiet gap
            # would emit one per bar and call it a strategy.
            last_at = res["closed_at"]
            per_day[closed_at.date()] = n_today + 1
            if not dry_run:
                session.add(Signal(**row))
                await session.commit()
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    ap.add_argument("--loop", type=int, default=0,
                    help="repeat every N seconds instead of exiting (0 = one pass)")
    ap.add_argument("--catchup-bars", type=int, default=DEFAULT_CATCHUP_BARS,
                    help="how many recently-closed bars one pass may repair")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    eng = create_async_engine(os.environ["DATABASE_URL"])
    Session = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    try:
        while True:
            try:
                async with Session() as s:
                    res = await run_once(s, a.symbol, a.lookback_days,
                                         a.dry_run, a.catchup_bars)
                print("engine producer: %s" % res, flush=True)
            except Exception as exc:            # a bad pass must not kill the loop
                print("engine producer pass failed: %s" % exc, flush=True)
                if not a.loop:
                    raise
            if not a.loop:
                return
            await asyncio.sleep(a.loop)
    finally:
        await eng.dispose()


if __name__ == "__main__":
    asyncio.run(main())
