#!/usr/bin/env python3
"""Restate `trades.realized_pl` onto the auditable basis (#234).

  python restate_book.py [--dry-run] [--apply] [--basis auditable|all]

WHAT IT DOES. `trades.realized_pl` is a derived column -- the monitor recomputes
it from the trade's closed legs on every tick. 555 of those legs carry money
that was never settled against their own position (517 of them identical to the
cent to a leg in a DIFFERENT trade), so the sum includes 53,068.3 AED that
belongs to somebody else's close. This recomputes the column counting only legs
whose P&L is auditable.

WHAT IT IS NOT. It is not a correction. The excluded legs really traded; we
simply cannot say what they made, and the restated book is therefore
INCOMPLETE. It reads ~53k better, and that improvement is not the bot doing
better -- it is us no longer counting what we cannot verify. Every number this
prints is shown next to what it left out, for that reason.

REVERSIBLE. `legs.realized_pl` is not touched, so `--basis all` recomputes the
original totals exactly. Nothing here destroys the ability to go back.

DELIBERATELY A SCRIPT, not a startup backfill. A migration that moves reported
P&L by 53k the moment it deploys is a decision taken by whoever ran the deploy,
which is nobody. This one has to be typed.
"""
import argparse
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

BASES = ("auditable", "all")

# `is_auditable` in SQL: exact, or NULL for a row the classifier never reached.
# Kept as one string so the report and the UPDATE cannot drift apart.
AUDITABLE_PRED = "(l.pl_attribution IS NULL OR l.pl_attribution = 'exact')"

REPORT = """
SELECT t.account_id,
       round(sum(l.realized_pl)::numeric, 1)                        AS as_reported,
       round(sum(l.realized_pl) FILTER (WHERE {pred})::numeric, 1)  AS auditable,
       round(sum(l.realized_pl) FILTER (WHERE NOT {pred})::numeric, 1) AS excluded_pl,
       count(*)                                                     AS legs,
       count(*) FILTER (WHERE NOT {pred})                           AS excluded_legs
FROM legs l JOIN trades t ON t.id = l.trade_id
WHERE l.status = 'closed' AND l.realized_pl IS NOT NULL
GROUP BY 1 ORDER BY 1
""".format(pred=AUDITABLE_PRED)

# Only rows that would actually change, so a second run reports 0 and writes
# nothing. `IS DISTINCT FROM` rather than `<>` because either side can be NULL.
UPDATE = """
UPDATE trades t SET realized_pl = c.total
FROM (SELECT l.trade_id, coalesce(sum(l.realized_pl) FILTER (WHERE {pred}), 0) AS total
        FROM legs l WHERE l.status = 'closed' AND l.realized_pl IS NOT NULL
       GROUP BY l.trade_id) c
WHERE c.trade_id = t.id AND t.realized_pl IS DISTINCT FROM c.total
"""

UPDATE_ALL = """
UPDATE trades t SET realized_pl = c.total
FROM (SELECT l.trade_id, coalesce(sum(l.realized_pl), 0) AS total
        FROM legs l WHERE l.status = 'closed' AND l.realized_pl IS NOT NULL
       GROUP BY l.trade_id) c
WHERE c.trade_id = t.id AND t.realized_pl IS DISTINCT FROM c.total
"""

LEDGER = """
SELECT account_id, round(sum(realized_pl)::numeric, 1) AS trades_ledger,
       count(*) AS trades
FROM trades WHERE realized_pl IS NOT NULL GROUP BY 1 ORDER BY 1
"""

UNCLASSIFIED = """
SELECT count(*) FROM legs
 WHERE status = 'closed' AND realized_pl IS NOT NULL AND pl_attribution IS NULL
"""


def _table(rows, cols):
    widths = [max(len(c), *(len(str(r[i])) for r in rows)) if rows else len(c)
              for i, c in enumerate(cols)]
    line = "  ".join(c.rjust(w) for c, w in zip(cols, widths))
    out = [line, "  ".join("-" * w for w in widths)]
    for r in rows:
        out.append("  ".join(str(v).rjust(w) for v, w in zip(r, widths)))
    return "\n".join(out)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the restated totals (default is a dry run)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--basis", default="auditable", choices=BASES,
                    help="'all' recomputes the ORIGINAL totals — the way back")
    a = ap.parse_args()
    if a.apply and a.dry_run:
        sys.exit("--apply and --dry-run are opposites; pick one")

    eng = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with eng.begin() as c:
            n_unclassified = (await c.execute(text(UNCLASSIFIED))).scalar()
            if n_unclassified:
                # Those legs would be COUNTED (NULL reads as auditable), which
                # is right for history the classifier never reached and wrong
                # if the classifier simply has not run yet. Say so rather than
                # restating on a half-labelled book.
                print("WARNING: %d closed legs are still unclassified; they will "
                      "be counted as auditable. Has the #234 backfill run?"
                      % n_unclassified)

            res = await c.execute(text(REPORT))
            cols = list(res.keys())
            rows = [tuple("" if v is None else v for v in r) for r in res.all()]
            print("\nPer account, from the legs:\n")
            print(_table(rows, cols))

            tot_rep = sum(float(r[1] or 0) for r in rows)
            tot_aud = sum(float(r[2] or 0) for r in rows)
            tot_exc = sum(float(r[3] or 0) for r in rows)
            n_exc = sum(int(r[5] or 0) for r in rows)
            print("\n  as reported   %12.1f" % tot_rep)
            print("  auditable     %12.1f" % tot_aud)
            print("  EXCLUDED      %12.1f  across %d legs" % (tot_exc, n_exc))
            print("\n  The book reads %.1f better on the auditable basis. That is"
                  "\n  not the bot doing better — it is %d legs whose money we"
                  "\n  cannot verify no longer being counted. They really traded."
                  % (tot_aud - tot_rep, n_exc))

            if not a.apply:
                print("\nDry run — nothing written. Re-run with --apply.")
                return

            stmt = UPDATE_ALL if a.basis == "all" else UPDATE
            r = await c.execute(text(stmt.format(pred=AUDITABLE_PRED)))
            print("\nApplied basis=%s — %d trade rows changed." % (a.basis, r.rowcount))

            res = await c.execute(text(LEDGER))
            print("\nTrades ledger now:\n")
            print(_table([tuple(x) for x in res.all()], list(res.keys())))
    finally:
        await eng.dispose()


asyncio.run(main())
