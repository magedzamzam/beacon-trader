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


def _structure_membership(features: dict) -> tuple:
    """(in_fvg, in_ob): is the entry price inside an UNFILLED FVG / UNMITIGATED
    OB on any captured timeframe? The structure keys embed their params
    (e.g. "fvg_0.25_50"), so match by prefix (#59)."""
    in_fvg = in_ob = False
    for _tf, block in (features or {}).items():
        if not isinstance(block, dict):
            continue
        for key, val in block.items():
            if not isinstance(val, dict):
                continue
            inside = bool(val.get("present")) and val.get("dist_pct") == 0
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
