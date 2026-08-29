#!/usr/bin/env python3
"""Pull 1m bid/ask bars from Capital.com straight into public.candles.

Replaces the CSV hop in history_price.py. Reuses the broker credentials already
stored (encrypted) in `brokers.credentials_ref`, so no new secrets are needed.

  python candle_sync.py [--symbol XAUUSD] [--since ISO] [--until ISO]
                        [--broker-id N] [--window-minutes 480] [--dry-run]

Resumes from MAX(ts) in the DB by default. Idempotent: ON CONFLICT DO NOTHING on
uq_candle_ts, so re-running never duplicates and never rewrites history.
"""
import argparse, asyncio, json, sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from beacon_core.brokers.capital_com import CapitalComAdapter
from beacon_core.brokers.registry import get_adapter, resolve_credentials
from beacon_core.brokers.types import BrokerError
from beacon_core.logging import get_logger
from beacon_core.worker import market_closed, run_loop, staleness_exit, worker_engine

log = get_logger("candle-sync")

MAX_BARS = 1000
# The provider's own resolution vocabulary is the adapter's (#257); this script
# only needs to know how long each bar is and what we call the timeframe.
RES_MIN = dict(CapitalComAdapter.RESOLUTIONS)
TF_NAME = {"MINUTE": "1m", "MINUTE_5": "5m", "MINUTE_15": "15m", "MINUTE_30": "30m",
           "HOUR": "1h", "HOUR_4": "4h", "DAY": "1d"}


def classify(c):
    """`BrokerCandle` -> (row_tuple, quality) or (None, reason).

    The DB's CHECK constraints (ck_candle_hl, ck_candle_oc) will REJECT a bad bar
    and abort the batch, so violators are skipped here and counted -- never
    silently repaired (#169). This is candle-STORE policy and stays above the
    adapter (#257): the adapter's job is to hand over the provider's bars in a
    provider-neutral shape, not to decide which of them this store will keep."""
    v = [c.open_bid, c.open_ask, c.high_bid, c.high_ask,
         c.low_bid, c.low_ask, c.close_bid, c.close_ask]
    if c.ts is None or any(x is None for x in v):
        return None, "incomplete"
    ob, oa, hb, ha, lb, la, cb, ca = [float(x) for x in v]
    if hb < lb or ha < la:
        return None, "ck_candle_hl"
    if hb < max(ob, cb) or lb > min(ob, cb):
        return None, "ck_candle_oc"
    # Crossed-quote test uses OPEN and CLOSE only. `high_ask`/`high_bid` are the
    # extremes of two SEPARATE series and need not occur in the same instant, so
    # `high_ask - high_bid < 0` is a range artifact, not a crossed quote -- the
    # same reason services/replay/README.md says only the close spread is a
    # spread you can reason about. Flagging on it marks good bars 'suspect', and
    # `quality != 'ok'` bars are dropped from every replay.
    quality = "suspect" if (oa < ob or ca < cb) else "ok"
    ts = c.ts.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
    return (ts, ob, oa, hb, ha, lb, la, cb, ca,
            float(c.volume or 0), round(ca - cb, 6), quality), None


async def sync_once(a):
    """One catch-up pass. Resumes from MAX(ts); idempotent via ON CONFLICT."""
    tf = TF_NAME[a.resolution]

    async with worker_engine() as eng:
        return await _sync_with_engine(a, eng, tf)


async def _sync_with_engine(a, eng, tf):
    async with eng.connect() as conn:
        q = ("select id,name,is_demo,credentials_ref,type "
             "from public.brokers where enabled")
        if a.broker_id:
            q += " and id=%d" % a.broker_id
        br = (await conn.exec_driver_sql(q + " order by is_demo desc limit 1")).fetchone()
        if not br:
            sys.exit("no enabled broker")
        epic = (await conn.exec_driver_sql(
            "select broker_epic from public.symbol_maps where internal_symbol='%s' "
            "and broker_id=%d limit 1" % (a.symbol, br[0]))).scalar()
        if not epic:
            sys.exit("no symbol_maps row for " + a.symbol)
        mx = (await conn.exec_driver_sql(
            "select max(ts) from public.candles where symbol='%s' and timeframe='%s'"
            % (a.symbol, tf))).scalar()
    creds = resolve_credentials(br[3] if isinstance(br[3], dict) else json.loads(br[3]))
    is_demo = bool(br[2])
    creds.setdefault("is_demo", is_demo)
    source = "capital:" + ("demo" if is_demo else "live")

    start = (datetime.strptime(a.since, "%Y-%m-%dT%H:%M:%S") if a.since
             else (mx.replace(tzinfo=None) + timedelta(minutes=RES_MIN[a.resolution])
                   if mx else datetime(2026, 1, 1)))
    end = (datetime.strptime(a.until, "%Y-%m-%dT%H:%M:%S") if a.until
           else datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0, second=0))
    log.info("broker=%s (type=%s is_demo=%s) epic=%s symbol=%s tf=%s source=%s",
             br[1], br[4], is_demo, epic, a.symbol, tf, source)
    log.info("db max ts = %s; fetching %s -> %s", mx, start, end)
    if start >= end:
        log.info("nothing to do -- already current"); return

    # #257: the bars come through the BrokerAdapter, which already owns this
    # provider's hostnames, session tokens, 401 re-auth and 429 back-off. This
    # script used to carry a second, weaker copy of all of it.
    cap = get_adapter(br[4], creds)
    win = timedelta(minutes=a.window_minutes)
    cursor, rows, skipped, empties, nwin, errs = start, [], Counter(), 0, 0, Counter()
    try:
        while cursor < end:
            wend = min(cursor + win, end)
            try:
                bars = await cap.fetch_candles(epic, a.resolution, cursor, wend,
                                               max_bars=MAX_BARS)
            except BrokerError as exc:
                # A window that will not load is one window, not the whole pass:
                # the rest is still worth having and the count is reported below
                # rather than swallowed.
                errs[type(exc).__name__] += 1
                bars = []
            nwin += 1
            if not bars:
                empties += 1
            for b in bars:
                row, reason = classify(b)
                if row is None:
                    skipped[reason] += 1
                else:
                    rows.append(row)
            if nwin % 10 == 0:
                log.info("window %d cursor=%s rows=%d", nwin, cursor, len(rows))
            cursor = wend
            await asyncio.sleep(a.throttle)
    finally:
        await cap.aclose()

    log.info("fetched %d insertable bars over %d windows (%d empty)",
             len(rows), nwin, empties)
    if skipped:
        log.warning("SKIPPED (constraint violations / incomplete, NOT repaired): %s",
                    dict(skipped))
    log.info("quality: %s", dict(Counter(r[-1] for r in rows)))
    if errs:
        log.warning("windows that ERRORED (not fetched): %s", dict(errs))
    if a.dry_run:
        log.info("--dry-run: nothing written"); return
    if not rows:
        log.info("nothing to insert"); return

    ins = 0
    async with eng.begin() as conn:
        for i in range(0, len(rows), 500):
            chunk = rows[i:i + 500]
            vals = ",".join(
                "('%s','%s','%s+00',%r,%r,%r,%r,%r,%r,%r,%r,%r,%r,'%s','%s')"
                % (a.symbol, tf, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8],
                   r[9], r[10], source, r[11]) for r in chunk)
            res = await conn.exec_driver_sql(
                "INSERT INTO public.candles (symbol,timeframe,ts,open_bid,open_ask,"
                "high_bid,high_ask,low_bid,low_ask,close_bid,close_ask,volume,"
                "spread_nominal,source,quality) VALUES " + vals +
                " ON CONFLICT (symbol,timeframe,ts) DO NOTHING")
            ins += res.rowcount
    log.info("INSERTED %d new bars (%d were already present)", ins, len(rows) - ins)
    async with eng.connect() as conn:
        r = (await conn.exec_driver_sql(
            "select count(*), min(ts), max(ts) from public.candles where symbol='%s' "
            "and timeframe='%s'" % (a.symbol, tf))).fetchone()
        log.info("candles now: n=%s  %s -> %s", r[0], r[1], r[2])

async def _freshness_exit(a) -> int:
    """0 when the store is current, 1 when it has gone stale (#224).

    The QUERY is here because "stale" means something different per store; the
    JUDGEMENT and its vocabulary are shared (#258, `worker.staleness_exit`), so
    three probes cannot answer the same question three different ways."""
    tf = TF_NAME[a.resolution]
    async with worker_engine() as eng, eng.connect() as conn:
        mx = (await conn.exec_driver_sql(
            "select max(ts) from public.candles where symbol='%s' and timeframe='%s'"
            % (a.symbol, tf))).scalar()
    if mx is None:
        return staleness_exit(age_min=None, limit_min=a.healthcheck,
                              subject="newest bar",
                              missing="no bars at all for %s %s" % (a.symbol, tf))
    now = datetime.now(timezone.utc)
    return staleness_exit(
        age_min=(now - mx).total_seconds() / 60.0,
        limit_min=a.healthcheck,
        subject="newest bar %s" % mx,
        # Gold prints nothing from Friday 21:00Z to Sunday 22:00Z, so an
        # unguarded age check goes red every weekend, gets muted, and a real
        # stall then hides behind the mute.
        closed=market_closed(now))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--resolution", default="MINUTE", choices=sorted(RES_MIN))
    ap.add_argument("--broker-id", type=int, default=None)
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--window-minutes", type=int, default=480)
    ap.add_argument("--throttle", type=float, default=0.25)
    ap.add_argument("--dry-run", action="store_true")
    # #224: the store went 15 days stale because this ran by hand and then did
    # not. --loop makes it a service instead of a habit. One pass then exit
    # remains the default, so every existing manual invocation is unchanged.
    ap.add_argument("--loop", type=int, default=0,
                    help="repeat every N seconds instead of exiting (0 = one pass)")
    # This service's health IS the freshness of the store. Probing "is the
    # process up?" would have reported green through the entire 15-day stall
    # that #224 is about, because the process was not up at all -- nobody had
    # started it. Probe the thing that matters instead.
    ap.add_argument("--healthcheck", type=int, default=0, metavar="MAX_AGE_MIN",
                    help="exit 0 if the newest bar is younger than N minutes, else 1")
    a = ap.parse_args()
    if a.healthcheck:
        sys.exit(await _freshness_exit(a))
    await run_loop(lambda: sync_once(a), name="candle-sync", interval=a.loop, logger=log)


asyncio.run(main())
