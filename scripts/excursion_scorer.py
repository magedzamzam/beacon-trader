#!/usr/bin/env python3
"""Score every signal's forward excursion on a schedule (#232).

`analysis/excursion_store.recompute` is what turns a signal — traded, skipped,
filtered or shadow — into a forward-R label. It existed only behind a POST
endpoint that nothing called, so it ran when somebody remembered: 394 signals
sat unscored for 15 days and three consecutive weeklies had to report that no
per-source exit argument could be made. This is the same rot `candle-sync` had
and the same fix: make it a service, and probe the STORE rather than the
process.

  python excursion_scorer.py [--symbol XAUUSD] [--days 30] [--loop SECONDS]
                             [--healthcheck MAX_UNSCORED_AGE_MIN] [--dry-run]

WHY IT MATTERS NOW: the engine (#224) writes `status='shadow'` signals that are
never traded, so the excursion label is the ONLY measurement they will ever
have. Writing the row is not the experiment — scoring it is.

Windowed by `--days` rather than replaying all of history every pass. The
horizon is 1440 one-minute bars, so a signal older than a day cannot change its
own label; re-deriving 2026-01 on every pass would buy nothing and cost the
whole candle table's worth of arithmetic. Rows outside the window are left
exactly as they are — `recompute` upserts per signal and never deletes.
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from beacon_core.analysis import excursion_store as EXS

DEFAULT_DAYS = 30

# A signal is UNSCORED if it has no `signal`-basis excursion row. Three shapes
# are excluded because no amount of scoring will ever produce one, and a probe
# that counted them would go red permanently and be ignored:
#   * no geometry — `entry_from`/`sl` NULL, counted as `no_geometry` and skipped
#   * no coverage — the candles do not reach it yet; that is candle-sync's
#     health to report, and it clears itself when they do
#   * older than the scoring window, which this service does not claim
UNSCORED_SQL = """
SELECT count(*) AS n,
       EXTRACT(EPOCH FROM (now() - min(s.created_at))) / 60 AS oldest_min
FROM signals s
LEFT JOIN signal_excursions e
       ON e.signal_id = s.id AND e.basis = 'signal'
WHERE e.id IS NULL
  AND s.symbol = :symbol
  AND s.entry_from IS NOT NULL AND s.sl IS NOT NULL
  AND s.created_at > now() - make_interval(days => :days)
  AND COALESCE(s.signal_at, s.created_at) < (
        SELECT max(ts) FROM candles
         WHERE symbol = :symbol AND timeframe = '1m' AND quality = 'ok')
"""


async def score_once(session, a) -> dict:
    frm = datetime.now(timezone.utc) - timedelta(days=a.days)
    if a.dry_run:
        return {"dry_run": True, "frm": frm.isoformat()}
    return await EXS.recompute(session, symbol=a.symbol, frm=frm)


def summarise(res: dict) -> str:
    """The line an operator can read at a glance, including the gate.

    `agreement_sl` is the reason to look: it is the label's own check against
    broker-recorded stop-outs, and a run that silently drifts below it is one
    whose numbers should stop being quoted."""
    if res.get("dry_run"):
        return "dry-run, would score from %s" % res["frm"]
    ag = res.get("agreement_sl") or {}
    cov = res.get("coverage") or {}
    return ("signals %s · resolved %s · written %s · capped %s · no_coverage %s "
            "· agreement_sl %s (n=%s) %s · bars to %s"
            % (res.get("signals"), res.get("resolved"), res.get("written"),
               res.get("horizon_capped"), res.get("no_coverage"),
               ag.get("rate"), ag.get("n"),
               "PASS" if ag.get("passed") else "FAIL", cov.get("last")))


async def _unscored_exit(a) -> int:
    """Health IS the absence of a scoring backlog.

    A liveness probe would have been green through the entire 15-day stall,
    because nothing was running at all."""
    eng = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with eng.connect() as c:
            row = (await c.execute(text(UNSCORED_SQL),
                                   {"symbol": a.symbol, "days": a.days})).one()
    finally:
        await eng.dispose()
    n, oldest = int(row[0] or 0), float(row[1] or 0.0)
    if n == 0:
        print("OK: nothing scoreable is unscored")
        return 0
    if oldest > a.healthcheck:
        print("UNHEALTHY: %d scoreable signal(s) unscored, oldest %.0f min "
              "(limit %d)" % (n, oldest, a.healthcheck))
        return 1
    print("OK: %d unscored, oldest %.0f min — within the next pass" % (n, oldest))
    return 0


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help="score signals created within the last N days")
    ap.add_argument("--loop", type=int, default=0,
                    help="repeat every N seconds instead of exiting (0 = one pass)")
    ap.add_argument("--healthcheck", type=int, default=0, metavar="MAX_AGE_MIN",
                    help="exit 0 if no scoreable signal has been unscored longer "
                         "than N minutes, else 1")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.healthcheck:
        sys.exit(await _unscored_exit(a))

    eng = create_async_engine(os.environ["DATABASE_URL"])
    Session = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    try:
        while True:
            try:
                async with Session() as s:
                    print("excursion scorer: %s" % summarise(await score_once(s, a)),
                          flush=True)
            except Exception as exc:         # a bad pass must not kill the loop
                print("excursion scorer pass failed: %s" % exc, flush=True)
                if not a.loop:
                    raise
            if not a.loop:
                return
            print("--- sleeping %ds ---" % a.loop, flush=True)
            await asyncio.sleep(a.loop)
    finally:
        await eng.dispose()


asyncio.run(main())
