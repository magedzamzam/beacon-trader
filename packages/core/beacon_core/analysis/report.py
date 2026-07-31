"""Signal ↔ channel ↔ regime correlation report (#53) — the payoff that turns
the shadow sidecar into decisions. Answers "which channel works in which
regime" from the labelled join signal_analytics → signals → trades.realized_pl,
with Beta-Binomial credible intervals (reuses analysis.bayes) so small-n buckets
are shrunk toward the base rate instead of over-trusted.

Read-only / observability. Epoch-awareness caveat (per #51): stats are pooled
across the whole history — a config change creates a regime break the caller
should weigh before acting.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from .bayes import posterior
from ._util import dig_num, adverse_side
from ..logging import get_logger

log = get_logger("analytics.report")

# numeric estimator fields to summarise for a feature→outcome read
_FEATURE_PATHS = {
    "hurst": ("hurst", "value"),
    "adx": ("regime", "adx"),
    "atr_pct": ("regime", "atr_pct"),
    "realized_vol": ("regime", "realized_vol"),
    "kalman_slope": ("kalman", "slope"),
    "vwap_z": ("vwap_deviation", "z"),
}




def _summary(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {"n": len(vals), "mean": round(sum(vals) / len(vals), 4)}


# Closed-trade floor for a per-channel verdict to be "significant" (§4). Correlated
# signals mean effective-N << raw-N, so treat this as a floor, not a guarantee.
SIGNIFICANCE_N = 30


def channel_verdict_rollup(rows, significance_n: int = SIGNIFICANCE_N) -> dict:
    """Pure synthesis behind /analytics/synthesis — the "so what?" layer (#117).

    Pools the labelled analytics→trade join PER CHANNEL (across regimes) into a
    keep / watch / cut read with an EXPLICIT significance state, so a page can lead
    with a decision instead of asking the operator to assemble one from five tables.
    DB-free so the reduction is unit-testable on a bare box (repo convention).

    This is NOT a new estimator: it reuses `posterior` and reduces existing outputs
    into a verdict. `rows`: iterable of {channel, realized_pl}. A channel is
    `significant` at n≥significance_n, `watch` from ceil(sig/2)..sig-1, else
    `gathering`. Verdict is keep (ci_low>base) / cut (ci_high<base) / hold
    (straddles base) only once significant; watch/gathering are explicitly
    provisional. Shadow — nothing gates on this."""
    watch_n = max(1, (significance_n + 1) // 2)
    buckets = defaultdict(lambda: {"n": 0, "wins": 0, "pl": 0.0})
    overall_n = overall_wins = 0
    for r in rows:
        pl = r.get("realized_pl")
        if pl is None:
            continue
        pl = float(pl)
        win = pl > 0
        chan = r.get("channel") or "Unattributed"
        b = buckets[chan]
        b["n"] += 1
        b["wins"] += 1 if win else 0
        b["pl"] += pl
        overall_n += 1
        overall_wins += 1 if win else 0
    base = (overall_wins / overall_n) if overall_n else 0.5

    def _verdict(n, ci_low, ci_high):
        if n < watch_n:
            return "gathering", "gathering"
        if n < significance_n:
            return "watch", "watch"
        if ci_low > base:
            return "significant", "keep"
        if ci_high < base:
            return "significant", "cut"
        return "significant", "hold"

    channels = []
    for chan, b in buckets.items():
        post = posterior(b["wins"], b["n"], base)
        state, verdict = _verdict(b["n"], post["ci_low"], post["ci_high"])
        channels.append({
            "channel": chan, "n": b["n"], "wins": b["wins"],
            "win_rate": round(b["wins"] / b["n"], 4) if b["n"] else None,
            "expectancy": round(b["pl"] / b["n"], 4) if b["n"] else None,
            "ci_low": round(post["ci_low"], 4), "ci_high": round(post["ci_high"], 4),
            "state": state, "verdict": verdict,
        })
    # significant first, then most reliably-good (highest lower bound), then size
    order = {"significant": 0, "watch": 1, "gathering": 2}
    channels.sort(key=lambda c: (order[c["state"]], -c["ci_low"], -c["n"]))
    n_sig = sum(1 for c in channels if c["state"] == "significant")
    any_edge = any(c["verdict"] in ("keep", "cut") for c in channels)
    return {
        "significance_n": significance_n, "watch_n": watch_n,
        "base_rate": round(base, 4), "n_labelled": overall_n,
        "n_channels": len(channels), "n_significant": n_sig,
        "any_credible_edge": any_edge, "channels": channels,
        "note": ("Weekly channel verdict (#117): labelled trades pooled per channel; "
                 "keep/watch/cut from the 90%% credible interval vs the base rate. "
                 "Significant only at n>=%d closed (§4 — correlated signals mean "
                 "effective-N << raw-N). Shadow — nothing gates on this." % significance_n),
    }


def _channel_verdict_query():
    """The labelled analytics→trade join behind channel_verdict_report, factored
    out so it's compile-testable on a bare box. Anchors the FROM on SignalAnalytics
    EXPLICITLY: unlike channel_regime_report this select carries no SignalAnalytics
    column, so SQLAlchemy can't infer the join's left side ("Can't determine which
    FROM clause to join from") without select_from."""
    from sqlalchemy import select
    from ..db.models import SignalAnalytics, Signal, Source, Trade
    return (select(Source.name, Trade.realized_pl)
            .select_from(SignalAnalytics)
            .join(Signal, Signal.id == SignalAnalytics.signal_id)
            .join(Trade, Trade.signal_id == SignalAnalytics.signal_id)
            .outerjoin(Source, Source.id == Signal.source_id))


async def channel_verdict_report(session, frm=None, to=None,
                                 significance_n: int = SIGNIFICANCE_N) -> dict:
    """Async wrapper (#117): the labelled analytics→trade join pooled per channel
    into the keep/watch/cut synthesis. Same join and SIGNAL-time [frm, to) anchor
    as `channel_regime_report`, so the verdict can't drift from the detail table it
    summarises. Read-only / shadow."""
    from ..db.models import Signal

    q = _channel_verdict_query()
    if frm is not None:
        q = q.where(Signal.created_at >= frm)
    if to is not None:
        q = q.where(Signal.created_at < to)
    rows = (await session.execute(q)).all()
    return channel_verdict_rollup(
        [{"channel": name, "realized_pl": pl} for name, pl in rows],
        significance_n=significance_n)


async def channel_regime_report(session, frm=None, to=None) -> dict:
    """Per-channel × regime performance + regime mix by channel + a
    win/loss feature read, all off the labelled analytics→trade join.
    Optional [frm, to) window anchored on the SIGNAL time (Signal.created_at) —
    the time the report groups by (#58)."""
    from sqlalchemy import select
    from ..db.models import SignalAnalytics, Signal, Source, Trade

    q = (select(Source.name, SignalAnalytics.regime, SignalAnalytics.analytics,
                Trade.realized_pl)
         .join(Signal, Signal.id == SignalAnalytics.signal_id)
         .join(Trade, Trade.signal_id == SignalAnalytics.signal_id)
         .outerjoin(Source, Source.id == Signal.source_id))
    if frm is not None:
        q = q.where(Signal.created_at >= frm)
    if to is not None:
        q = q.where(Signal.created_at < to)
    rows = (await session.execute(q)).all()

    buckets = defaultdict(lambda: {"n": 0, "wins": 0, "pl": 0.0})
    chan_regime = defaultdict(lambda: defaultdict(int))
    feat_win = defaultdict(list)
    feat_loss = defaultdict(list)
    overall_n = overall_wins = 0

    for name, regime, analytics, pl in rows:
        if pl is None:
            continue
        pl = float(pl)
        win = pl > 0
        chan = name or "Unattributed"
        reg = regime or "unknown"
        b = buckets[(chan, reg)]
        b["n"] += 1
        b["wins"] += 1 if win else 0
        b["pl"] += pl
        chan_regime[chan][reg] += 1
        overall_n += 1
        overall_wins += 1 if win else 0
        for fname, path in _FEATURE_PATHS.items():
            v = dig_num(analytics or {}, *path)
            if v is not None:
                (feat_win if win else feat_loss)[fname].append(v)

    base = (overall_wins / overall_n) if overall_n else 0.5

    by_cr = []
    for (chan, reg), b in buckets.items():
        post = posterior(b["wins"], b["n"], base)
        by_cr.append({
            "channel": chan, "regime": reg, "n": b["n"],
            "win_rate": round(b["wins"] / b["n"], 4),
            "expectancy": round(b["pl"] / b["n"], 4),
            "ci_low": round(post["ci_low"], 4), "ci_high": round(post["ci_high"], 4),
        })
    # most reliably-good first (highest lower credible bound), then by size
    by_cr.sort(key=lambda r: (-r["ci_low"], -r["n"]))

    features = {f: {"win": _summary(feat_win.get(f, [])),
                    "loss": _summary(feat_loss.get(f, []))}
                for f in _FEATURE_PATHS}

    return {
        "base_rate": round(base, 4),
        "n_labelled": overall_n,
        "by_channel_regime": by_cr,
        "regime_mix_by_channel": {c: dict(m) for c, m in chan_regime.items()},
        "feature_by_outcome": features,
        "note": ("Shadow analytics — observability only, nothing gates on this. "
                 "Stats pooled across history; weigh config-change regime breaks."),
    }


async def execution_geometry_ab_report(session, frm=None, to=None,
                                       source_id=None) -> dict:
    """Payoff-geometry A/B read (#80 item 3 / #85 action 2), normalized to
    **R-multiples** so the arms are comparable even when they trade different
    nominal sizes.

    Each closed trade's outcome is expressed as R = realized_pl / planned_risk
    (planned_risk = the worst-case loss at the ORIGINAL stop, account ccy). R is
    scale-free, so it dissolves the equity-parity confound (#85 §2): a drawn-down
    arm and a fresh arm sizing the SAME signal at 1% of very different equities
    still get the same R denominator, so raw-AED P&L incomparability goes away.

    Arms are keyed by **account** (the A/B axis: e.g. acct#5 = Arm A `BE@TP1`,
    acct#7 = Arm B `BE@TP2`); the exit `strategy` label(s) actually seen on each
    arm's trades are surfaced for attribution. Optional [frm, to) window is
    anchored on SIGNAL time — both arms fan out from the same signals, so a window
    selects the same signal set for every arm. Optional `source_id` scopes to one
    channel (the A/B is run per-channel).

    Per arm it reports the geometry levers #80 targets:
      * avg_R / expectancy_R           — the bottom line in risk units
      * payoff_ratio  = avgWinR/|avgLossR|   (the ~0.56 → ~0.9 lever)
      * profit_factor = ΣwinR/|ΣlossR|       (scale-free)
      * breakeven_leg_rate             — the primary MECHANISM (#85): legs the
                                         TP1→entry ratchet dragged back to flat
      * pct_winners_reach_tp3          — did winners actually run (any tp_hit leg
                                         at tp_index ≥ 3), or get cut at TP1/2
    Win-rate carries a Beta-Binomial credible interval (small-n honesty, §4).
    Trades whose planned_risk is missing/zero keep their win/leg contribution but
    are excluded from R-based stats (R undefined) and counted in `n_no_risk`.

    Read-only / shadow — nothing gates on this; it is the measurement the #80
    experiment is judged by. Trade-level realized_pl + leg OUTCOME labels only
    (never legs.realized_pl — the cross-attribution bug, golden rule §5)."""
    from sqlalchemy import select
    from ..db.models import Trade, Signal, Account, ExecutionStrategy, Leg

    q = (select(Trade.id, Trade.account_id, Account.name, Trade.realized_pl,
                Trade.planned_risk, Trade.strategy_id, ExecutionStrategy.label)
         .join(Signal, Signal.id == Trade.signal_id)
         .outerjoin(Account, Account.id == Trade.account_id)
         .outerjoin(ExecutionStrategy, ExecutionStrategy.id == Trade.strategy_id)
         .where(Trade.status == "closed"))
    if source_id is not None:
        q = q.where(Signal.source_id == source_id)
    if frm is not None:
        q = q.where(Signal.created_at >= frm)
    if to is not None:
        q = q.where(Signal.created_at < to)
    trows = [{"trade_id": tid, "account_id": aid, "account": aname,
              "realized_pl": pl, "planned_risk": pr, "strategy_label": slabel}
             for tid, aid, aname, pl, pr, sid, slabel in (await session.execute(q)).all()]

    tids = [t["trade_id"] for t in trows]
    lrows = []
    if tids:
        lq = select(Leg.trade_id, Leg.outcome, Leg.tp_index).where(Leg.trade_id.in_(tids))
        lrows = [{"trade_id": tid, "outcome": outcome, "tp_index": tp_index}
                 for tid, outcome, tp_index in (await session.execute(lq)).all()]
    return geometry_ab_rollup(trows, lrows, source_id=source_id)


# Leg outcomes that represent a resolved close (counted in the leg denominator).
_RESOLVED_OUTCOMES = ("tp_hit", "sl_hit", "breakeven", "manual", "expired")


def geometry_ab_rollup(trades, legs, source_id=None) -> dict:
    """Pure roll-up behind execution_geometry_ab_report — kept DB-free so the
    geometry math is unit-testable on a bare box (the repo's test convention).

    `trades`: iterable of dicts {trade_id, account_id, account, realized_pl,
    planned_risk, strategy_label}. `legs`: iterable of {trade_id, outcome,
    tp_index}. See execution_geometry_ab_report for the metric definitions."""
    from collections import defaultdict

    def _arm():
        return {"n": 0, "wins": 0, "net": 0.0, "n_r": 0, "n_no_risk": 0,
                "sum_r": 0.0, "sum_win_r": 0.0, "sum_loss_r": 0.0,
                "n_win_r": 0, "n_loss_r": 0, "labels": set(),
                "legs": 0, "be_legs": 0, "winners": 0, "winners_tp3": 0}

    arms = defaultdict(_arm)
    trade_arm = {}          # trade_id -> account_id
    trade_win = {}          # trade_id -> bool
    a_name = {}             # account_id -> account name
    overall_n = overall_wins = 0

    for t in trades:
        pl = t.get("realized_pl")
        if pl is None:
            continue
        pl = float(pl)
        win = pl > 0
        acct_id = t.get("account_id")
        tid = t.get("trade_id")
        a = arms[acct_id]
        a["n"] += 1
        a["wins"] += 1 if win else 0
        a["net"] += pl
        if win:
            a["winners"] += 1
        if t.get("strategy_label"):
            a["labels"].add(t["strategy_label"])
        if acct_id is not None and acct_id not in a_name and t.get("account"):
            a_name[acct_id] = t["account"]
        trade_arm[tid] = acct_id
        trade_win[tid] = win
        overall_n += 1
        overall_wins += 1 if win else 0
        prisk = t.get("planned_risk")
        r = None
        if prisk is not None and float(prisk) != 0:
            r = pl / abs(float(prisk))
        if r is None:
            a["n_no_risk"] += 1
        else:
            a["n_r"] += 1
            a["sum_r"] += r
            if win:
                a["sum_win_r"] += r
                a["n_win_r"] += 1
            else:
                a["sum_loss_r"] += r
                a["n_loss_r"] += 1

    # Legs of the selected trades: breakeven-leg rate + did a winner reach ≥TP3.
    winners_tp3 = set()                       # trade_ids whose winner ran to ≥TP3
    for lg in legs:
        tid = lg.get("trade_id")
        if tid not in trade_arm:
            continue
        acct_id = trade_arm[tid]
        a = arms[acct_id]
        outcome, tp_index = lg.get("outcome"), lg.get("tp_index")
        # Only count legs with a resolved outcome as "closed legs".
        if outcome in _RESOLVED_OUTCOMES:
            a["legs"] += 1
            if outcome == "breakeven":
                a["be_legs"] += 1
        if outcome == "tp_hit" and (tp_index or 0) >= 3 and trade_win.get(tid):
            winners_tp3.add(tid)
    for tid in winners_tp3:
        arms[trade_arm[tid]]["winners_tp3"] += 1

    base = (overall_wins / overall_n) if overall_n else 0.5

    def _fmt(acct_id, a):
        n, nr = a["n"], a["n_r"]
        post = posterior(a["wins"], n, base)
        avg_win_r = (a["sum_win_r"] / a["n_win_r"]) if a["n_win_r"] else None
        avg_loss_r = (a["sum_loss_r"] / a["n_loss_r"]) if a["n_loss_r"] else None
        payoff = (avg_win_r / abs(avg_loss_r)) if (avg_win_r is not None
                  and avg_loss_r not in (None, 0)) else None
        pf = (a["sum_win_r"] / abs(a["sum_loss_r"])) if a["sum_loss_r"] < 0 else None
        return {
            "account_id": acct_id,
            "account": a_name.get(acct_id) or (f"acct#{acct_id}" if acct_id else "unmapped"),
            "arms": sorted(a["labels"]),
            "n_trades": n, "n_with_risk": nr, "n_no_risk": a["n_no_risk"],
            "win_rate": round(a["wins"] / n, 4) if n else None,
            "win_rate_ci": [round(post["ci_low"], 4), round(post["ci_high"], 4)],
            "avg_R": round(a["sum_r"] / nr, 4) if nr else None,
            "expectancy_R": round(a["sum_r"] / nr, 4) if nr else None,
            "avg_win_R": round(avg_win_r, 4) if avg_win_r is not None else None,
            "avg_loss_R": round(avg_loss_r, 4) if avg_loss_r is not None else None,
            "payoff_ratio": round(payoff, 4) if payoff is not None else None,
            "profit_factor": round(pf, 4) if pf is not None else None,
            "breakeven_leg_rate": round(a["be_legs"] / a["legs"], 4) if a["legs"] else None,
            "n_legs": a["legs"], "n_breakeven_legs": a["be_legs"],
            "pct_winners_reach_tp3": round(a["winners_tp3"] / a["winners"], 4) if a["winners"] else None,
            "net_nominal": round(a["net"], 2),
        }

    by_arm = [_fmt(acct_id, a) for acct_id, a in arms.items()]
    by_arm.sort(key=lambda r: (r["account_id"] is None, r["account_id"]))

    return {
        "n_closed": overall_n, "base_rate": round(base, 4),
        "source_id": source_id, "by_arm": by_arm,
        "note": ("Payoff-geometry A/B in R-multiples (#80/#85). R = realized_pl / "
                 "planned_risk (scale-free → dissolves the equity-parity confound). "
                 "payoff_ratio = avgWinR/|avgLossR| (the ~0.56→~0.9 lever); "
                 "breakeven_leg_rate is the primary mechanism (legs the TP1→entry "
                 "ratchet dragged to flat); pct_winners_reach_tp3 = winners that "
                 "actually ran. Trade-level P&L + leg outcome labels only (§5). "
                 "Shadow/read-only; judge only at N≥30 closed/arm (§4)."),
    }


# How close to an unfilled zone still counts as "at" it, in percent of price.
# `dist_pct == 0` means literally inside the band, and measured over the whole
# capture (#168) that lands on 28/386 signals for FVG and **5/386 for Order
# Block** — a bucket that needs a year to reach N>=30 and can never be ruled on.
# The zone is a band, not a line, and an entry a hundredth of a percent outside
# it is the same trade; 0.05% of gold is ~$2, well inside the spread-plus-slippage
# an entry actually lands in. Set to 0.0 to recover the strict inside-only test.
STRUCTURE_NEAR_PCT = 0.05


def _structure_membership(features: dict, near_pct: float = STRUCTURE_NEAR_PCT) -> tuple:
    """(in_fvg, in_ob): is the entry price inside — or within `near_pct` of — an
    UNFILLED FVG / UNMITIGATED OB on any captured timeframe? The structure keys
    embed their params (e.g. "fvg_0.25_50"), so match by prefix (#59).

    `present` is load-bearing and stays required: the indicator falls back to
    reporting the nearest FILLED gap when no unfilled one exists, so dropping it
    would count mitigated zones as live ones."""
    in_fvg = in_ob = False
    for _tf, block in (features or {}).items():
        if not isinstance(block, dict):
            continue
        for key, val in block.items():
            if not isinstance(val, dict):
                continue
            dist = val.get("dist_pct")
            inside = (bool(val.get("present"))
                      and isinstance(dist, (int, float))
                      and not isinstance(dist, bool)
                      and dist <= near_pct)
            if key.startswith("fvg"):
                in_fvg = in_fvg or inside
            elif key.startswith("order_block"):
                in_ob = in_ob or inside
    return in_fvg, in_ob


async def structure_outcome_report(session, frm=None, to=None) -> dict:
    """The FVG/OB-vs-outcome cut (#59): win-rate & expectancy when a signal's
    entry sits inside an unfilled Fair Value Gap / unmitigated Order Block vs not,
    overall and per channel/regime, with Beta-Binomial credible intervals. Joins
    signal_features (structure) + signal_analytics (regime) -> trades.realized_pl.
    Shadow only — nothing gates on it."""
    from collections import defaultdict
    from sqlalchemy import select
    from ..db.models import SignalFeature, SignalAnalytics, Signal, Source, Trade

    q = (select(Source.name, SignalFeature.features, SignalAnalytics.regime,
                Trade.realized_pl)
         .join(Signal, Signal.id == SignalFeature.signal_id)
         .join(Trade, Trade.signal_id == SignalFeature.signal_id)
         .outerjoin(SignalAnalytics, SignalAnalytics.signal_id == SignalFeature.signal_id)
         .outerjoin(Source, Source.id == Signal.source_id))
    if frm is not None:
        q = q.where(Signal.created_at >= frm)
    if to is not None:
        q = q.where(Signal.created_at < to)
    rows = (await session.execute(q)).all()

    def _cell():
        return {"n": 0, "wins": 0, "pl": 0.0}

    agg = {"fvg": defaultdict(lambda: defaultdict(_cell)),
           "ob": defaultdict(lambda: defaultdict(_cell))}
    overall_n = overall_wins = 0

    for name, feats, regime, pl in rows:
        if pl is None:
            continue
        pl = float(pl)
        win = pl > 0
        overall_n += 1
        overall_wins += 1 if win else 0
        in_fvg, in_ob = _structure_membership(feats)
        chan = name or "Unattributed"
        reg = regime or "unknown"
        for struct, inside in (("fvg", in_fvg), ("ob", in_ob)):
            mem = "inside" if inside else "outside"
            for scope, label in (("overall", "all"), ("channel", chan), ("regime", reg)):
                b = agg[struct][(scope, label)][mem]
                b["n"] += 1
                b["wins"] += 1 if win else 0
                b["pl"] += pl

    base = (overall_wins / overall_n) if overall_n else 0.5

    def _rows(struct):
        out = []
        for (scope, label), mems in agg[struct].items():
            for mem, b in mems.items():
                if not b["n"]:
                    continue
                post = posterior(b["wins"], b["n"], base)
                out.append({"scope": scope, "label": label, "membership": mem,
                            "n": b["n"], "win_rate": round(b["wins"] / b["n"], 4),
                            "expectancy": round(b["pl"] / b["n"], 4),
                            "ci_low": round(post["ci_low"], 4),
                            "ci_high": round(post["ci_high"], 4)})
        out.sort(key=lambda r: (r["scope"] != "overall", r["label"], r["membership"]))
        return out

    return {
        "n_labelled": overall_n, "base_rate": round(base, 4),
        "fvg": _rows("fvg"), "ob": _rows("ob"),
        "note": ("Structure (FVG/OB) vs outcome — SHADOW only, measure-before-gate "
                 "(#59). 'inside' = entry price within an unfilled FVG / unmitigated "
                 "OB on any captured timeframe. Small-n: trust the credible interval."),
    }


async def execution_tax_report(session, frm=None, to=None, account_id=None) -> dict:
    """Per-channel win-rate under BOTH labels (#63), computed on the signals that
    carry both: the channel's SIGNAL-QUALITY outcome (its own claims — TP1+ vs SL,
    independent of our fills/stops) and our BOT-REALIZED outcome (realized_pl>0).
    The GAP = signal_quality_wr − bot_realized_wr is the *execution tax*: setups
    that worked but we didn't capture. Beta-Binomial credible intervals. Shadow /
    read-only — nothing gates on this; it sizes the execution-fix backlog."""
    from collections import defaultdict
    from sqlalchemy import select
    from ..db.models import Signal, Source, Trade, SignalClaim
    from .bayes import signal_quality_label

    tq = (select(Trade.signal_id, Trade.realized_pl, Source.name)
          .join(Signal, Signal.id == Trade.signal_id)
          .outerjoin(Source, Source.id == Signal.source_id)
          .where(Trade.status == "closed"))
    if account_id is not None:                  # #83: per-account A/B slice
        tq = tq.where(Trade.account_id == account_id)
    if frm is not None:
        tq = tq.where(Signal.created_at >= frm)
    if to is not None:
        tq = tq.where(Signal.created_at < to)
    trows = (await session.execute(tq)).all()

    sids = [sid for sid, pl, _ in trows if pl is not None and sid is not None]
    claims_by = defaultdict(list)
    if sids:
        for c in (await session.execute(
                select(SignalClaim).where(SignalClaim.signal_id.in_(sids)))).scalars().all():
            claims_by[c.signal_id].append(c)

    def _cell():
        return {"n": 0, "sq_wins": 0, "br_wins": 0}

    by_chan = defaultdict(_cell)
    overall = _cell()
    for sid, pl, name in trows:
        if pl is None:
            continue
        sq = signal_quality_label(claims_by.get(sid))
        if sq is None:                       # no clean channel outcome -> excluded
            continue
        br = float(pl) > 0
        for cell in (by_chan[name or "Unattributed"], overall):
            cell["n"] += 1
            cell["sq_wins"] += 1 if sq else 0
            cell["br_wins"] += 1 if br else 0

    on = overall["n"]
    base_sq = (overall["sq_wins"] / on) if on else 0.5
    base_br = (overall["br_wins"] / on) if on else 0.5

    def _fmt(cell):
        n = cell["n"]
        if not n:
            return None
        sq, br = posterior(cell["sq_wins"], n, base_sq), posterior(cell["br_wins"], n, base_br)
        return {"n": n,
                "signal_quality_wr": round(cell["sq_wins"] / n, 4),
                "sq_ci": [round(sq["ci_low"], 4), round(sq["ci_high"], 4)],
                "bot_realized_wr": round(cell["br_wins"] / n, 4),
                "br_ci": [round(br["ci_low"], 4), round(br["ci_high"], 4)],
                "execution_tax": round((cell["sq_wins"] - cell["br_wins"]) / n, 4)}

    by_channel = [{"channel": c, **_fmt(cell)} for c, cell in by_chan.items() if cell["n"]]
    by_channel.sort(key=lambda r: -r["execution_tax"])       # biggest tax first

    return {
        "n_labelled": on, "overall": _fmt(overall), "by_channel": by_channel,
        "note": ("Execution tax (#63) — signal-quality WR (channel claims: TP1+ "
                 "vs SL) minus bot-realized WR (realized_pl>0), on signals with "
                 "both labels. A positive gap = the setup worked but our execution "
                 "didn't capture it (fills #25 / TTL #40 / stops). Shadow only. "
                 "Ambiguous/contradictory claims are excluded, not counted as loss."),
    }


async def trend_alignment_outcome_report(session, frm=None, to=None, *,
                                         timeframe="4h", ema_period=200) -> dict:
    """The aligned-vs-counter split as a first-class metric (#72): win-rate,
    net PnL and expectancy for trend-ALIGNED vs COUNTER-trend entries, overall
    and per channel, with Beta-Binomial credible intervals. Classifies each
    labelled trade from its persisted `signal_features` (price vs `timeframe`
    EMA`ema_period` at signal time) — the exact definition the live filter (#48)
    gates on — joined to trades.realized_pl. Unknown-trend signals are excluded
    (the filter fails open on them). Read-only; nothing gates on this."""
    from collections import defaultdict
    from sqlalchemy import select
    from ..db.models import SignalFeature, Signal, Source, Trade
    from ..execution.trend_filter import alignment_from_features

    q = (select(Source.name, Signal.direction, SignalFeature.features, Trade.realized_pl)
         .join(Signal, Signal.id == SignalFeature.signal_id)
         .join(Trade, Trade.signal_id == SignalFeature.signal_id)
         .outerjoin(Source, Source.id == Signal.source_id))
    if frm is not None:
        q = q.where(Signal.created_at >= frm)
    if to is not None:
        q = q.where(Signal.created_at < to)
    rows = (await session.execute(q)).all()

    def _cell():
        return {"n": 0, "wins": 0, "pl": 0.0}

    overall = defaultdict(_cell)                       # "aligned" | "counter"
    by_channel = defaultdict(lambda: defaultdict(_cell))
    n_class = wins_class = n_unknown = 0

    for name, direction, feats, pl in rows:
        if pl is None:
            continue
        aligned = alignment_from_features(feats, direction, timeframe, ema_period)
        if aligned is None:                            # trend unknown -> fail-open, excluded
            n_unknown += 1
            continue
        pl = float(pl)
        win = pl > 0
        key = "aligned" if aligned else "counter"
        for b in (overall[key], by_channel[name or "Unattributed"][key]):
            b["n"] += 1
            b["wins"] += 1 if win else 0
            b["pl"] += pl
        n_class += 1
        wins_class += 1 if win else 0

    base = (wins_class / n_class) if n_class else 0.5

    def _fmt(b):
        post = posterior(b["wins"], b["n"], base)
        return {"n": b["n"], "win_rate": round(b["wins"] / b["n"], 4),
                "net": round(b["pl"], 2), "expectancy": round(b["pl"] / b["n"], 4),
                "ci_low": round(post["ci_low"], 4), "ci_high": round(post["ci_high"], 4)}

    return {
        "timeframe": timeframe, "ema_period": ema_period,
        "n_labelled": n_class, "n_unknown_trend": n_unknown, "base_rate": round(base, 4),
        "overall": {k: _fmt(v) for k, v in overall.items() if v["n"]},
        "by_channel": {ch: {k: _fmt(v) for k, v in m.items() if v["n"]}
                       for ch, m in by_channel.items()},
        "note": ("Trend-alignment (price vs %s EMA%d at signal time) vs outcome — "
                 "SHADOW metric (#72). 'counter' = entry fighting the higher-TF "
                 "trend; #48 filter skips/de-sizes these. Unknown-trend signals "
                 "excluded (filter fails open). Small-n: trust the credible interval."
                 % (timeframe, ema_period)),
    }


def _zone_proximity_band(sm: dict, direction: str):
    """(proximity_band, adverse) from a structure_magnet block: how close is the
    nearest magnet zone, and is it on the ADVERSE side (BUY into a zone above /
    SELL into one below)?"""
    nz = (sm or {}).get("nearest_zone") or {}
    if nz.get("inside"):
        band = "inside"
    else:
        d = nz.get("dist_atr")
        band = ("near" if d is not None and d <= 0.5 else
                "mid" if d is not None and d <= 2.0 else "far")
    side = nz.get("side")
    adverse = adverse_side(direction, side)
    return band, bool(adverse)


async def structure_magnet_outcome_report(session, frm=None, to=None) -> dict:
    """Phase-2 payoff (#61): does magnet proximity / HTF-structure alignment
    predict outcome? Cuts win-rate & expectancy by `htf_alignment`, by nearest-
    zone proximity band, and by adverse-side, with Beta-Binomial credible
    intervals — off the signal_analytics(structure_magnet) -> trades join. This is
    the measurement Phase-3 filtering waits on (measure-before-gate). Shadow only."""
    from collections import defaultdict
    from sqlalchemy import select
    from ..db.models import SignalAnalytics, Signal, Trade

    q = (select(SignalAnalytics.analytics, SignalAnalytics.direction, Trade.realized_pl)
         .join(Trade, Trade.signal_id == SignalAnalytics.signal_id)
         .join(Signal, Signal.id == SignalAnalytics.signal_id))
    if frm is not None:
        q = q.where(Signal.created_at >= frm)
    if to is not None:
        q = q.where(Signal.created_at < to)
    rows = (await session.execute(q)).all()

    def _cell():
        return {"n": 0, "wins": 0, "pl": 0.0}

    cuts = {"htf_alignment": defaultdict(_cell), "proximity": defaultdict(_cell),
            "adverse_side": defaultdict(_cell)}
    overall_n = overall_wins = 0

    for analytics, direction, pl in rows:
        sm = (analytics or {}).get("structure_magnet")
        if pl is None or not sm:
            continue
        pl = float(pl)
        win = pl > 0
        overall_n += 1
        overall_wins += 1 if win else 0
        band, adverse = _zone_proximity_band(sm, direction)
        for dim, key in (("htf_alignment", sm.get("htf_alignment") or "unknown"),
                         ("proximity", band),
                         ("adverse_side", "adverse" if adverse else "clear")):
            b = cuts[dim][key]
            b["n"] += 1
            b["wins"] += 1 if win else 0
            b["pl"] += pl

    base = (overall_wins / overall_n) if overall_n else 0.5

    def _rows(dim):
        out = []
        for key, b in cuts[dim].items():
            if not b["n"]:
                continue
            post = posterior(b["wins"], b["n"], base)
            out.append({"bucket": key, "n": b["n"],
                        "win_rate": round(b["wins"] / b["n"], 4),
                        "expectancy": round(b["pl"] / b["n"], 4),
                        "ci_low": round(post["ci_low"], 4),
                        "ci_high": round(post["ci_high"], 4)})
        out.sort(key=lambda r: (-r["ci_low"], -r["n"]))
        return out

    return {
        "n_labelled": overall_n, "base_rate": round(base, 4),
        "htf_alignment": _rows("htf_alignment"),
        "proximity": _rows("proximity"),
        "adverse_side": _rows("adverse_side"),
        "note": ("Magnet/structure vs outcome — SHADOW only (#61). Phase-3 "
                 "filtering waits on this: enable structure.filter once a bucket's "
                 "credible interval separates from the base rate at N>=30."),
    }


# --- Per-channel excursion / R-ladder (#182) ----------------------------------
def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2.0


def excursion_rollup(rows, significance_n: int = SIGNIFICANCE_N,
                     ladder=(0.25, 0.5, 1.0, 1.5, 2.0, 3.0)) -> dict:
    """Per-channel R-ladder from the reconstructed excursions (#182). Pure — DB-free
    so the reduction is unit-testable on a bare box (repo convention).

    `rows`: iterable of {channel, tp1_r, mfe_r, mae_r, race, ladder, horizon_capped}.

    THE TABLE THIS EXISTS FOR pairs two columns that must be read together:
    `median_tp1_r` — how far a channel puts its own first target, measured in that
    signal's own risk — and `reach[X]` — how often price actually travelled X*R
    before the original stop. A channel whose ladder says it reaches 0.5R often but
    1.0R rarely, while its own TP1 sits at 1.0R, is telling you to take profit
    earlier ON THAT SOURCE. That is a per-(account, source) exit config change
    justified by a measure that does not depend on how we exited.

    It also retro-actively qualifies every per-channel win rate reported so far:
    median TP1 ranges 0.15R to 1.00R across sources, a 6.7x difference in how far
    price must move to "win", so a binary TP1-vs-SL rate compares channels on
    unequal terms. Shadow — nothing gates on this."""
    watch_n = max(1, (significance_n + 1) // 2)
    keys = [f"{float(x):g}" if float(x) % 1 else f"{float(x):.1f}" for x in ladder]
    buckets = defaultdict(lambda: {"tp1_r": [], "mfe_r": [], "mae_r": [],
                                   "tp1_first": 0, "sl_first": 0, "horizon": 0,
                                   "reach": defaultdict(int), "n": 0})
    for r in rows:
        mfe = r.get("mfe_r")
        if mfe is None:
            continue
        b = buckets[r.get("channel") or "Unattributed"]
        b["n"] += 1
        b["mfe_r"].append(float(mfe))
        if r.get("mae_r") is not None:
            b["mae_r"].append(float(r["mae_r"]))
        if r.get("tp1_r") is not None:
            b["tp1_r"].append(float(r["tp1_r"]))
        race = r.get("race")
        b["tp1_first" if race == "tp1" else "sl_first" if race == "sl" else "horizon"] += 1
        rung = r.get("ladder") or {}
        for k in keys:
            if rung.get(k):
                b["reach"][k] += 1

    channels = []
    for chan, b in buckets.items():
        n = b["n"]
        state = ("significant" if n >= significance_n
                 else "watch" if n >= watch_n else "gathering")
        channels.append({
            "channel": chan, "n": n, "state": state,
            "median_tp1_r": round(_median(b["tp1_r"]), 4) if b["tp1_r"] else None,
            "median_mfe_r": round(_median(b["mfe_r"]), 4),
            "median_mae_r": round(_median(b["mae_r"]), 4) if b["mae_r"] else None,
            "tp1_before_sl": round(b["tp1_first"] / n, 4),
            "sl_first": round(b["sl_first"] / n, 4),
            "unresolved": round(b["horizon"] / n, 4),
            "reach": {k: round(b["reach"][k] / n, 4) for k in keys},
        })
    order = {"significant": 0, "watch": 1, "gathering": 2}
    channels.sort(key=lambda c: (order[c["state"]], -c["n"]))
    total = sum(c["n"] for c in channels)
    return {
        "significance_n": significance_n, "watch_n": watch_n,
        "ladder": keys, "n_labelled": total, "n_channels": len(channels),
        "channels": channels,
        "note": ("Per-channel R-ladder (#182): P(price reached X*R in the called "
                 "direction before the ORIGINAL stop), reconstructed from 1m bid/ask "
                 "candles — exit-independent. Read `reach` against `median_tp1_r`: a "
                 "channel that rarely reaches its own TP1 distance wants an earlier "
                 "target, not a verdict on its signals. TP1 distance varies ~6.7x "
                 "across sources, so it also qualifies every binary win rate reported "
                 "so far. Not significant below n>=%d (§4). Shadow — nothing gates on "
                 "this." % significance_n),
    }


async def excursion_report(session, frm=None, to=None,
                           basis: str = "signal",
                           significance_n: int = SIGNIFICANCE_N) -> dict:
    """The per-channel R-ladder off the persisted `signal_excursions` join.
    SIGNAL-time [frm, to) anchor, matching the other channel reports so the
    tables can't drift. Read-only / shadow.

    Defaults to the `signal` basis — the channel's stated entry — because that is
    the one that covers signals we skipped or never filled, which is the whole
    point of an exit-independent label."""
    from sqlalchemy import select
    from ..db.models import Signal, SignalExcursion, Source

    q = (select(Source.name, SignalExcursion.tp1_r, SignalExcursion.mfe_r,
                SignalExcursion.mae_r, SignalExcursion.race,
                SignalExcursion.ladder, SignalExcursion.horizon_capped)
         .select_from(SignalExcursion)
         .join(Signal, Signal.id == SignalExcursion.signal_id)
         .outerjoin(Source, Source.id == Signal.source_id)
         .where(SignalExcursion.basis == basis))
    if frm is not None:
        q = q.where(Signal.created_at >= frm)
    if to is not None:
        q = q.where(Signal.created_at < to)
    rows = (await session.execute(q)).all()
    out = excursion_rollup(
        [{"channel": name, "tp1_r": tp1_r, "mfe_r": mfe, "mae_r": mae,
          "race": race, "ladder": ladder, "horizon_capped": capped}
         for name, tp1_r, mfe, mae, race, ladder, capped in rows],
        significance_n=significance_n)
    out["basis"] = basis
    return out


# --- Shadow strategies: Monte Carlo geometry null + Turtle breakout -----------
# The bucket edges for the Monte Carlo calibration curve. A null that is well
# calibrated should land close to the diagonal; systematic deviation means the
# vol estimate or the horizon is wrong, not that the channels have edge.
_MC_BUCKETS = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]


def shadow_strategy_rollup(rows, significance_n: int = SIGNIFICANCE_N) -> dict:
    """Pure reduction behind /analytics/shadow-strategies. DB-free so it is
    unit-testable on a bare box (repo convention).

    `rows`: iterable of {channel, realized_pl, planned_risk, montecarlo, turtle}.

    THE NUMBER THIS EXISTS FOR is `montecarlo.edge` — realized win-rate MINUS the
    win-rate the signal's own SL/TP geometry implies with no skill assumed. A
    channel posting a far stop and a near target wins most of its trades by
    arithmetic; only the excess over its own null is evidence of anything. The
    same applies in R: `actual_mean_r` vs `null_mean_r`.

    Turtle splits the same trades by whether the mechanical Donchian system
    agreed with the channel's direction — a channel that only wins when the
    breakout system already agreed is not adding much over a free rule.
    """
    mc_n = mc_wins = mc_truncated = 0
    mc_pred_sum = mc_null_r_sum = 0.0
    mc_r_n = 0
    mc_r_sum = 0.0
    mc_by_channel = defaultdict(lambda: {"n": 0, "wins": 0, "pred": 0.0})
    buckets = [{"lo": lo, "hi": hi, "n": 0, "wins": 0, "pred": 0.0}
               for lo, hi in _MC_BUCKETS]

    def _cell():
        return {"n": 0, "wins": 0, "pl": 0.0}

    tu_overall = defaultdict(_cell)                    # "agrees" | "disagrees"
    tu_by_channel = defaultdict(lambda: defaultdict(_cell))
    tu_unknown = 0
    tu_diverges = 0

    for row in rows:
        pl = row.get("realized_pl")
        if pl is None:
            continue
        pl = float(pl)
        win = pl > 0
        channel = row.get("channel") or "Unattributed"

        mc = row.get("montecarlo") or {}
        p = mc.get("p_win_geometry")
        if isinstance(p, (int, float)):
            p = float(p)
            mc_n += 1
            mc_wins += 1 if win else 0
            mc_pred_sum += p
            if mc.get("horizon_truncated"):
                mc_truncated += 1
            c = mc_by_channel[channel]
            c["n"] += 1
            c["wins"] += 1 if win else 0
            c["pred"] += p
            for b in buckets:
                if b["lo"] <= p < b["hi"]:
                    b["n"] += 1
                    b["wins"] += 1 if win else 0
                    b["pred"] += p
                    break
            er = mc.get("expected_r")
            risk = row.get("planned_risk")
            try:
                risk = float(risk) if risk is not None else None
            except (TypeError, ValueError):
                risk = None
            if isinstance(er, (int, float)) and risk and risk > 0:
                mc_r_n += 1
                mc_r_sum += pl / risk
                mc_null_r_sum += float(er)

        tu = row.get("turtle") or {}
        agrees = tu.get("agrees")
        if tu.get("diverges"):
            tu_diverges += 1
        if agrees is None:
            tu_unknown += 1
        else:
            key = "agrees" if agrees else "disagrees"
            for b in (tu_overall[key], tu_by_channel[channel][key]):
                b["n"] += 1
                b["wins"] += 1 if win else 0
                b["pl"] += pl

    base = (mc_wins / mc_n) if mc_n else 0.5

    def _fmt(b):
        post = posterior(b["wins"], b["n"], base)
        return {"n": b["n"], "win_rate": round(b["wins"] / b["n"], 4),
                "net": round(b["pl"], 2), "expectancy": round(b["pl"] / b["n"], 4),
                "ci_low": round(post["ci_low"], 4), "ci_high": round(post["ci_high"], 4)}

    def _mc_fmt(b):
        actual = b["wins"] / b["n"]
        pred = b["pred"] / b["n"]
        post = posterior(b["wins"], b["n"], pred)
        return {"n": b["n"], "actual_win_rate": round(actual, 4),
                "geometry_win_rate": round(pred, 4), "edge": round(actual - pred, 4),
                "ci_low": round(post["ci_low"], 4), "ci_high": round(post["ci_high"], 4),
                "beats_null": post["ci_low"] > pred, "significant": b["n"] >= significance_n}

    montecarlo = {
        "n": mc_n,
        # Signals whose horizon was too short for the race to resolve. Their
        # geometry win-rate is UNDERSTATED, which inflates `edge` — so a large
        # count here has to be fixed (raise analytics.montecarlo.horizon_bars)
        # before any edge below is read as skill.
        "n_horizon_truncated": mc_truncated,
        "actual_win_rate": round(mc_wins / mc_n, 4) if mc_n else None,
        "geometry_win_rate": round(mc_pred_sum / mc_n, 4) if mc_n else None,
        "edge": round(mc_wins / mc_n - mc_pred_sum / mc_n, 4) if mc_n else None,
        "n_with_r": mc_r_n,
        "actual_mean_r": round(mc_r_sum / mc_r_n, 4) if mc_r_n else None,
        "null_mean_r": round(mc_null_r_sum / mc_r_n, 4) if mc_r_n else None,
        "r_edge": round((mc_r_sum - mc_null_r_sum) / mc_r_n, 4) if mc_r_n else None,
        "by_channel": {ch: _mc_fmt(c) for ch, c in mc_by_channel.items() if c["n"]},
        "calibration": [{"bucket": "%.1f–%.1f" % (b["lo"], min(b["hi"], 1.0)),
                         **_mc_fmt(b)} for b in buckets if b["n"]],
    }
    turtle = {
        "n_unknown": tu_unknown, "n_diverges": tu_diverges,
        "overall": {k: _fmt(v) for k, v in tu_overall.items() if v["n"]},
        "by_channel": {ch: {k: _fmt(v) for k, v in m.items() if v["n"]}
                       for ch, m in tu_by_channel.items()},
    }
    return {
        "n_labelled": mc_n, "base_rate": round(base, 4),
        "significance_n": significance_n,
        "montecarlo": montecarlo, "turtle": turtle,
        "note": ("SHADOW — neither gates. Monte Carlo `edge` = realized win-rate "
                 "MINUS the win-rate the signal's own SL/TP geometry implies with "
                 "no skill assumed; a raw win-rate on a far stop and a near target "
                 "is arithmetic, not edge. `beats_null` requires the 90 percent "
                 "credible lower bound to clear that per-bucket null. `null_mean_r` "
                 "is ~0 by construction (a driftless null breaks even on EVERY "
                 "geometry), so it is a calibration check, not a ranking — and "
                 "because costs are not modelled, a channel merely MATCHING its "
                 "null is losing the spread. Turtle splits the same trades by "
                 "whether the 55-bar Donchian system agreed with the channel. "
                 "Don't act below N>=" + str(significance_n) + "."),
    }


async def shadow_strategy_report(session, frm=None, to=None) -> dict:
    """The labelled join behind /analytics/shadow-strategies: persisted per-signal
    `montecarlo` / `turtle` estimator blocks vs realized trade outcome. Read-only."""
    from sqlalchemy import select
    from ..db.models import SignalAnalytics, Signal, Source, Trade

    q = (select(Source.name, SignalAnalytics.analytics,
                Trade.realized_pl, Trade.planned_risk)
         .join(Signal, Signal.id == SignalAnalytics.signal_id)
         .join(Trade, Trade.signal_id == SignalAnalytics.signal_id)
         .outerjoin(Source, Source.id == Signal.source_id))
    if frm is not None:
        q = q.where(Signal.created_at >= frm)
    if to is not None:
        q = q.where(Signal.created_at < to)
    rows = (await session.execute(q)).all()
    return shadow_strategy_rollup([
        {"channel": name, "realized_pl": pl, "planned_risk": risk,
         "montecarlo": (an or {}).get("montecarlo"),
         "turtle": (an or {}).get("turtle")}
        for name, an, pl, risk in rows])


# --- Turtle exit counterfactual (#170) ----------------------------------------
def _timeframe_delta(timeframe: str):
    """One bar's wall-clock width, for sizing the channel warm-up window."""
    import datetime as _dt
    mins = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
    return _dt.timedelta(minutes=mins.get(timeframe, 60))


def _stderr(vals):
    """Standard error of the mean. None below 2 points."""
    if len(vals) < 2:
        return None
    mu = sum(vals) / len(vals)
    var = sum((x - mu) ** 2 for x in vals) / (len(vals) - 1)
    return (var / len(vals)) ** 0.5


def _mean_block(vals, significance_n, key="mean_delta_r"):
    """n / mean / stderr / `clear` for a list of R figures. `clear` is the honest
    bar: the mean has to beat its own spread before it says anything."""
    if not vals:
        return None
    mu = sum(vals) / len(vals)
    se = _stderr(vals)
    return {"n": len(vals), key: round(mu, 4),
            "stderr": round(se, 4) if se is not None else None,
            "significant": len(vals) >= significance_n,
            "clear": bool(se is not None and abs(mu) > se and len(vals) >= significance_n)}


def _exit_rule_block(rows, significance_n):
    """The ONLY population that can justify a close-at-market exit primitive:
    trades the Turtle BACKED at entry and then turned against (#171). For these,
    `delta_r` is a real exit-rule comparison — a different exit price for a trade
    we would have taken either way."""
    turned = [r for r in rows if r.get("backed_at_entry") is True
              and r.get("mechanism") == "flipped_mid_trade" and r.get("delta_r") is not None]
    backed = [r for r in rows if r.get("backed_at_entry") is True
              and r.get("delta_r") is not None]
    out = _mean_block([float(r["delta_r"]) for r in turned], significance_n)
    if out is None:
        return {"n": 0, "n_backed_at_entry": len(backed), "note": "no mid-trade turns yet"}
    out.update({
        "n_backed_at_entry": len(backed),
        "turn_rate": round(len(turned) / len(backed), 4) if backed else None,
        "mean_actual_r": round(sum(float(r["actual_r"]) for r in turned) / len(turned), 4),
        "mean_counterfactual_r": round(
            sum(float(r["counterfactual_r"]) for r in turned) / len(turned), 4),
        "helped": sum(1 for r in turned if float(r["delta_r"]) > 0),
        "hurt": sum(1 for r in turned if float(r["delta_r"]) < 0),
    })
    return out


def _entry_filter_block(rows, significance_n):
    """Trades the Turtle already opposed when they opened (#171). Skipping one
    means it is never taken, so its counterfactual is R = 0 EXACTLY — not an exit
    price one bar after entry. `mean_delta_r` here is therefore -mean_actual_r:
    the R that not trading these would have added.

    A strong result here points at the `turtle_signal` FILTRATION rule that
    already exists and is inert (#163) — a far cheaper change than an exit
    engine."""
    opposed = [r for r in rows if r.get("backed_at_entry") is False
               and r.get("actual_r") is not None]
    if not opposed:
        return {"n": 0, "note": "the Turtle opposed no trade at entry"}
    actual = [float(r["actual_r"]) for r in opposed]
    out = _mean_block([-a for a in actual], significance_n)
    out.update({
        "mean_actual_r": round(sum(actual) / len(actual), 4),
        "counterfactual_r": 0.0,                  # skipped -> no trade -> no R
        "win_rate": round(sum(1 for a in actual if a > 0) / len(actual), 4),
        "basis": "skip (R = 0), not an exit price",
    })
    return out


def _stop_distance_block(rows):
    """Is the effect just an artifact of stop distance? (#170 warned, #171 tests.)

    A 55-bar flip is a SLOW signal: it can only beat a stop that sits far away.
    Trades are split into risk-distance TERTILES computed from the sample itself,
    so there are no magic thresholds. If the delta lives entirely in the widest
    tertile, that is a finding about stop placement, not about the Turtle."""
    scored = [r for r in rows if r.get("risk") is not None and r.get("delta_r") is not None]
    if len(scored) < 6:
        return None
    ordered = sorted(scored, key=lambda r: float(r["risk"]))
    third = len(ordered) // 3
    bands = [("narrow", ordered[:third]), ("mid", ordered[third:2 * third]),
             ("wide", ordered[2 * third:])]
    out = {}
    for name, band in bands:
        if not band:
            continue
        deltas = [float(r["delta_r"]) for r in band]
        risks = [float(r["risk"]) for r in band]
        se = _stderr(deltas)
        out[name] = {"n": len(band),
                     "risk_lo": round(min(risks), 5), "risk_hi": round(max(risks), 5),
                     "mean_delta_r": round(sum(deltas) / len(deltas), 4),
                     "stderr": round(se, 4) if se is not None else None}
    return out


def turtle_exit_rollup(rows, significance_n: int = SIGNIFICANCE_N) -> dict:
    """Pure reduction behind /analytics/turtle-exit. DB-free, unit-testable.

    `rows`: iterable of {channel, direction, actual_r, counterfactual_r, delta_r,
    flipped}. Every R is PRICE-basis (see turtle.exit_counterfactual) so actual
    and counterfactual are comparable to each other. They are NOT the money
    figures on `trades.realized_pl`, which span a multi-leg ladder.

    The decision number is `mean_delta_r` — the R a flip-driven exit would have
    ADDED. It has to clear zero by more than `stderr_delta_r` at
    N>=significance_n before wiring a Turtle exit into the live SL engine is
    justified. `flip_rate` matters as much: a rule that rarely fires cannot help
    much however good its average looks.
    """
    def _cell():
        return {"n": 0, "flipped": 0, "actual": 0.0, "cf": 0.0,
                "delta": 0.0, "helped": 0, "hurt": 0}

    overall = _cell()
    by_channel = defaultdict(_cell)
    by_direction = defaultdict(_cell)
    deltas = []

    for row in rows:
        d, a, c = row.get("delta_r"), row.get("actual_r"), row.get("counterfactual_r")
        if d is None or a is None or c is None:
            continue
        d, a, c = float(d), float(a), float(c)
        flipped = bool(row.get("flipped"))
        for b in (overall, by_channel[row.get("channel") or "Unattributed"],
                  by_direction[(row.get("direction") or "?").upper()]):
            b["n"] += 1
            b["flipped"] += 1 if flipped else 0
            b["actual"] += a
            b["cf"] += c
            b["delta"] += d
            if flipped and d > 0:
                b["helped"] += 1
            elif flipped and d < 0:
                b["hurt"] += 1
        deltas.append(d)

    def _fmt(b):
        n = b["n"]
        if not n:
            return None
        return {"n": n, "n_flipped": b["flipped"],
                "flip_rate": round(b["flipped"] / n, 4),
                "mean_actual_r": round(b["actual"] / n, 4),
                "mean_counterfactual_r": round(b["cf"] / n, 4),
                "mean_delta_r": round(b["delta"] / n, 4),
                "helped": b["helped"], "hurt": b["hurt"],
                "significant": n >= significance_n}

    # A crude spread on the mean, so a large average off 4 trades cannot read as
    # a finding. NOT a credible interval — these deltas are not Bernoulli.
    stderr = _stderr(deltas)

    # --- #171: the blended mean is NOT readable, so split the two mechanisms ---
    rows = list(rows) if not isinstance(rows, list) else rows
    return {
        "significance_n": significance_n,
        "overall": _fmt(overall),
        "stderr_delta_r": round(stderr, 4) if stderr is not None else None,
        "exit_rule": _exit_rule_block(rows, significance_n),
        "entry_filter": _entry_filter_block(rows, significance_n),
        "by_stop_distance": _stop_distance_block(rows),
        "by_channel": {k: _fmt(v) for k, v in by_channel.items() if v["n"]},
        "by_direction": {k: _fmt(v) for k, v in by_direction.items() if v["n"]},
        "note": ("SHADOW backtest — replays the 55-bar Donchian across each trade's "
                 "holding period. READ `exit_rule` AND `entry_filter` SEPARATELY; "
                 "`overall` blends two different mechanisms and is not actionable "
                 "on its own (#171). `exit_rule` covers trades the Turtle BACKED at "
                 "entry and then turned against — only that can justify a "
                 "close-at-market exit primitive. `entry_filter` covers trades it "
                 "already opposed at entry, valued against R = 0 because skipping "
                 "means never taking them; a result there points at the inert "
                 "`turtle_signal` filtration rule, a far cheaper change. Check "
                 "`by_stop_distance` before believing either: a 55-bar flip is SLOW, "
                 "so an effect living entirely in the widest tertile is a finding "
                 "about stop placement. All R is PRICE-basis off the same entry and "
                 "risk distance, never trades.realized_pl. Act only when `clear` is "
                 "true (mean beats its own stderr at N>=" + str(significance_n) +
                 "). Costs are NOT modelled."),
    }


async def turtle_exit_report(session, adapter, broker_epic: str, *,
                             symbol: str = "XAUUSD", frm=None, to=None,
                             timeframe: str = "1h", window: int = 55,
                             variant: str = "signal", max_bars: int = 1000) -> dict:
    """Driver: ONE bar fetch, then every closed trade replayed against it.

    Every trade is the same instrument, so a single ranged `get_bars` covers the
    whole report instead of one call per trade. Bars start `window` periods
    before the earliest entry so the channel is warm at the first trade."""
    from sqlalchemy import select
    from ..db.models import Leg, Signal, Source, Trade
    from ..strategy.rules import entry_basis
    from ..ta.registry import TF_RESOLUTION
    from .turtle import exit_counterfactual

    def _empty(reason, n_trades=0):
        return {**turtle_exit_rollup([]), "symbol": symbol, "timeframe": timeframe,
                "variant": variant, "window": window, "n_trades": n_trades,
                "n_evaluated": 0, "worst_trades": [], "sample_trades": [],
                "skipped": {reason: n_trades}}

    # #176: the R denominator comes from the SIGNAL's stop, never from Leg.sl.
    # Leg.sl is the CURRENT stop and the ratchet mutates it, so a trade that
    # trailed to breakeven has |entry - leg.sl| ~ 0 and R explodes — 20% of legs
    # (all of them sl_moved, and all winners) had an effectively zero
    # denominator. This is the same immutable-stop rule #109 established for
    # be_lock_at_r; the monitor sources it identically (monitor/main.py:116).
    q = (select(Trade.id, Trade.direction, Source.name, Signal.sl)
         .join(Signal, Signal.id == Trade.signal_id)
         .outerjoin(Source, Source.id == Signal.source_id)
         .where(Trade.status == "closed"))
    if frm is not None:
        q = q.where(Signal.created_at >= frm)
    if to is not None:
        q = q.where(Signal.created_at < to)
    trades = {tid: {"direction": d, "channel": ch,
                    "signal_sl": float(sl) if sl is not None else None}
              for tid, d, ch, sl in (await session.execute(q)).all()}
    if not trades:
        return _empty("no_closed_trades")

    legs = (await session.execute(
        select(Leg.trade_id, Leg.entry, Leg.fill_price, Leg.close_price, Leg.sl,
               Leg.lot, Leg.created_at, Leg.closed_at)
        .where(Leg.trade_id.in_(list(trades)), Leg.status == "closed"))).all()

    # Collapse a trade's ladder into ONE position: earliest open -> latest close,
    # lot-weighted average exit price. Leg-level P&L is never read (CLAUDE.md §2.5)
    # — a close price is a price, not a P&L attribution.
    agg = {}
    for tid, entry, fill, close_px, sl, lot, created, closed in legs:
        if close_px is None or closed is None or lot is None:
            continue
        a = agg.setdefault(tid, {"opened": None, "closed": None, "num": 0.0,
                                 "den": 0.0, "entry": None})
        a["opened"] = created if a["opened"] is None else min(a["opened"], created)
        a["closed"] = closed if a["closed"] is None else max(a["closed"], closed)
        a["num"] += float(close_px) * float(lot)
        a["den"] += float(lot)
        if a["entry"] is None:
            # The leg still supplies the ENTRY basis — that is immutable and
            # correct. Only its `sl` is untrustworthy (#176).
            a["entry"] = float(entry_basis(fill, entry))

    # #171: account for EVERY trade that drops out, with a reason. 26% of closed
    # trades vanished from the first run with no explanation, which makes the
    # remaining sample impossible to trust.
    skipped = defaultdict(int)
    usable = {}
    for tid in trades:
        a = agg.get(tid)
        if a is None:
            skipped["no_closed_legs"] += 1
        elif a["den"] <= 0:
            skipped["zero_lot"] += 1
        elif not (a["opened"] and a["closed"]):
            skipped["missing_timestamps"] += 1
        elif not trades[tid].get("signal_sl"):
            # No original stop -> no honest R denominator. Skip and say so,
            # rather than dividing by a ratcheted one (#176).
            skipped["no_signal_stop"] += 1
        else:
            usable[tid] = a
    if not usable:
        return {**_empty("no_usable_legs", len(trades)), "skipped": dict(skipped)}

    earliest = min(a["opened"] for a in usable.values())
    latest = max(a["closed"] for a in usable.values())
    warm = _timeframe_delta(timeframe) * window * 3      # slack for closed hours
    bars = await adapter.get_bars(
        broker_epic, TF_RESOLUTION.get(timeframe, "HOUR"),
        from_ts=(earliest - warm).strftime("%Y-%m-%dT%H:%M:%S"),
        to_ts=latest.strftime("%Y-%m-%dT%H:%M:%S"), max_bars=max_bars)

    rows = []
    for tid, a in usable.items():
        meta = trades[tid]
        cf = exit_counterfactual(
            bars=bars, entry_time=a["opened"], exit_time=a["closed"],
            entry_price=a["entry"], sl_price=meta["signal_sl"],
            actual_exit_price=a["num"] / a["den"], direction=meta["direction"],
            window=window, variant=variant)
        if cf is None:
            skipped["counterfactual_unavailable"] += 1
            continue
        rows.append({"trade_id": tid, "channel": meta["channel"],
                     "direction": meta["direction"], **cf})

    out = turtle_exit_rollup(rows)
    # `worst_trades` is named for what it is: selected BY delta_r, so it is useless
    # for judging the distribution. `sample_trades` is ordered by trade id, which
    # is independent of the metric — that is the one to eyeball (#171).
    out.update({"symbol": symbol, "timeframe": timeframe, "variant": variant,
                "window": window, "n_trades": len(trades), "n_evaluated": len(rows),
                "n_bars": len(bars), "skipped": dict(skipped),
                "worst_trades": sorted(rows, key=lambda r: r["delta_r"])[:25],
                "sample_trades": sorted(rows, key=lambda r: r["trade_id"])[:25]})
    return out
