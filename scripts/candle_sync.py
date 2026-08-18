#!/usr/bin/env python3
"""Pull 1m bid/ask bars from Capital.com straight into public.candles.

Replaces the CSV hop in history_price.py. Reuses the broker credentials already
stored (encrypted) in `brokers.credentials_ref`, so no new secrets are needed.

  python candle_sync.py [--symbol XAUUSD] [--since ISO] [--until ISO]
                        [--broker-id N] [--window-minutes 480] [--dry-run]

Resumes from MAX(ts) in the DB by default. Idempotent: ON CONFLICT DO NOTHING on
uq_candle_ts, so re-running never duplicates and never rewrites history.
"""
import argparse, asyncio, json, os, sys, time, urllib.error, urllib.parse, urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import create_async_engine
from beacon_core.brokers.registry import resolve_credentials

HOSTS = {True: "https://demo-api-capital.backend-capital.com/api/v1",
         False: "https://api-capital.backend-capital.com/api/v1"}
TIME_FMT = "%Y-%m-%dT%H:%M:%S"
MAX_BARS = 1000
RES_MIN = {"MINUTE": 1, "MINUTE_5": 5, "MINUTE_15": 15, "MINUTE_30": 30,
           "HOUR": 60, "HOUR_4": 240, "DAY": 1440}
TF_NAME = {"MINUTE": "1m", "MINUTE_5": "5m", "MINUTE_15": "15m", "MINUTE_30": "30m",
           "HOUR": "1h", "HOUR_4": "4h", "DAY": "1d"}


class Cap:
    def __init__(self, base, creds):
        self.base, self.c = base, creds
        self.cst = self.xst = None
        self.ts = 0.0
        self.status = Counter()

    def _call(self, method, path, params=None, body=None, authed=True):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        h = {"X-CAP-API-KEY": self.c["api_key"], "Accept": "application/json"}
        if authed:
            h["CST"] = self.cst or ""
            h["X-SECURITY-TOKEN"] = self.xst or ""
        data = None
        if body is not None:
            data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, r.headers, json.loads(r.read().decode() or "{}")

    def auth(self):
        _, hdrs, info = self._call("POST", "/session", authed=False,
                                   body={"identifier": self.c["account_username"],
                                         "password": self.c["account_password"]})
        self.cst, self.xst = hdrs.get("CST"), hdrs.get("X-SECURITY-TOKEN")
        if not self.cst:
            sys.exit("auth returned no session tokens")
        self.ts = time.time(); time.sleep(1.0)
        return info

    def prices(self, epic, res, frm, to):
        for attempt in range(4):
            if not self.cst or time.time() - self.ts > 420:
                self.auth()
            try:
                st, _, d = self._call("GET", "/prices/" + epic,
                                      params={"resolution": res, "max": MAX_BARS,
                                              "from": frm.strftime(TIME_FMT),
                                              "to": to.strftime(TIME_FMT)})
                self.status[st] += 1
                return d.get("prices") or [], st, None
            except urllib.error.HTTPError as e:
                self.status[e.code] += 1
                err = e.read().decode("utf-8", "replace")[:200]
                if e.code in (401, 403): self.cst = None
                elif e.code == 429: time.sleep(2 ** attempt)
                # 400 won't fix on retry; 404 = no bars in this window (market
                # closed / weekend) and is a normal, expected answer -- retrying
                # it 4x just burns rate limit.
                elif e.code in (400, 404): return [], e.code, err
                else: time.sleep(1.5)
            except urllib.error.URLError as e:
                self.status[0] += 1; time.sleep(2)
        return [], None, "retries exhausted"


def classify(b):
    """-> (row_tuple, quality) or (None, reason). The DB's CHECK constraints
    (ck_candle_hl, ck_candle_oc) will REJECT a bad bar and abort the batch, so
    violators are skipped here and counted -- never silently repaired (#169)."""
    ts = (b.get("snapshotTimeUTC") or b.get("snapshotTime") or "").rstrip("Z")
    o, c_, h, l = (b.get("openPrice") or {}, b.get("closePrice") or {},
                   b.get("highPrice") or {}, b.get("lowPrice") or {})
    v = [o.get("bid"), o.get("ask"), h.get("bid"), h.get("ask"),
         l.get("bid"), l.get("ask"), c_.get("bid"), c_.get("ask")]
    if not ts or any(x is None for x in v):
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
    return (ts, ob, oa, hb, ha, lb, la, cb, ca,
            float(b.get("lastTradedVolume") or 0), round(ca - cb, 6), quality), None


async def sync_once(a):
    """One catch-up pass. Resumes from MAX(ts); idempotent via ON CONFLICT."""
    tf = TF_NAME[a.resolution]

    eng = create_async_engine(os.environ["DATABASE_URL"])
    async with eng.connect() as conn:
        q = "select id,name,is_demo,credentials_ref from public.brokers where enabled"
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
    source = "capital:" + ("demo" if is_demo else "live")

    start = (datetime.strptime(a.since, "%Y-%m-%dT%H:%M:%S") if a.since
             else (mx.replace(tzinfo=None) + timedelta(minutes=RES_MIN[a.resolution])
                   if mx else datetime(2026, 1, 1)))
    end = (datetime.strptime(a.until, "%Y-%m-%dT%H:%M:%S") if a.until
           else datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0, second=0))
    print("broker=%s (is_demo=%s)  epic=%s  symbol=%s tf=%s  source=%s"
          % (br[1], is_demo, epic, a.symbol, tf, source))
    print("db max ts = %s" % mx)
    print("fetching  %s -> %s" % (start, end))
    if start >= end:
        print("nothing to do -- already current"); return

    cap = Cap(HOSTS[is_demo], creds); cap.auth()
    win = timedelta(minutes=a.window_minutes)
    cursor, rows, skipped, empties, nwin = start, [], Counter(), 0, 0
    while cursor < end:
        wend = min(cursor + win, end)
        bars, st, err = cap.prices(epic, a.resolution, cursor, wend)
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
            print("  window %d  cursor=%s  rows=%d" % (nwin, cursor, len(rows)))
        cursor = wend
        time.sleep(a.throttle)

    print("\nfetched %d insertable bars over %d windows (%d empty)" % (len(rows), nwin, empties))
    if skipped:
        print("SKIPPED (constraint violations / incomplete, NOT repaired): %s" % dict(skipped))
    qc = Counter(r[-1] for r in rows)
    print("quality: %s" % dict(qc))
    print("http: %s" % dict(cap.status))
    if a.dry_run:
        print("\n--dry-run: nothing written"); return
    if not rows:
        print("nothing to insert"); return

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
    print("\nINSERTED %d new bars (%d were already present)" % (ins, len(rows) - ins))
    async with eng.connect() as conn:
        r = (await conn.exec_driver_sql(
            "select count(*), min(ts), max(ts) from public.candles where symbol='%s' "
            "and timeframe='%s'" % (a.symbol, tf))).fetchone()
        print("candles now: n=%s  %s -> %s" % (r[0], r[1], r[2]))
    await eng.dispose()

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
    a = ap.parse_args()
    while True:
        try:
            await sync_once(a)
        except Exception as exc:                 # a bad pass must not kill the loop
            print("sync pass failed: %s" % exc, flush=True)
            if not a.loop:
                raise
        if not a.loop:
            return
        print("--- sleeping %ds ---" % a.loop, flush=True)
        await asyncio.sleep(a.loop)


asyncio.run(main())
