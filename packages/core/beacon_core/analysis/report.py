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
                                       source_id=None,
                                       control_account_id=None) -> dict:
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
                Trade.planned_risk, Trade.strategy_id, ExecutionStrategy.label,
                Trade.signal_id, Signal.created_at, Trade.deployed_risk)
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
              "realized_pl": pl, "planned_risk": pr, "strategy_label": slabel,
              "signal_id": sigid, "signal_at": sat, "deployed_risk": dep}
             for tid, aid, aname, pl, pr, sid, slabel, sigid, sat, dep
             in (await session.execute(q)).all()]

    tids = [t["trade_id"] for t in trows]
    lrows = []
    if tids:
        lq = select(Leg.trade_id, Leg.outcome, Leg.tp_index).where(Leg.trade_id.in_(tids))
        lrows = [{"trade_id": tid, "outcome": outcome, "tp_index": tp_index}
                 for tid, outcome, tp_index in (await session.execute(lq)).all()]
    out = geometry_ab_rollup(trows, lrows, source_id=source_id)
    # #188: the de-lever verdict per arm, against the control. Emitted BESIDE the
    # existing keys — never in place of them; the weeklies grep those names.
    out["delever"] = delever_report(trows, control_account_id=control_account_id)
    return out


def delever_report(trades, *, control_account_id=None) -> dict:
    """Per-arm de-lever verdict against one control arm (#188).

    Pairs on SIGNAL, because both arms fan out from the same signals and only a
    signal traded by both says anything about the difference between them. The
    control defaults to the lowest account id present, which is the A/B's Arm A
    by convention; pass it explicitly when that is not true."""
    by_signal = {}
    for t in trades:
        sig = t.get("signal_id")
        if sig is None:
            continue
        by_signal.setdefault(sig, {})[t.get("account_id")] = t
    accounts = sorted({t.get("account_id") for t in trades
                       if t.get("account_id") is not None})
    if not accounts:
        return {"control_account_id": None, "arms": {}}
    control = control_account_id if control_account_id in accounts else accounts[0]

    def _r(t):
        pr, pl = t.get("planned_risk"), t.get("realized_pl")
        if pr in (None, 0) or pl is None or float(pr) == 0:
            return None
        return float(pl) / abs(float(pr))

    arms = {}
    for acct in accounts:
        if acct == control:
            continue
        pairs = []
        for sig, per_acct in by_signal.items():
            c, a = per_acct.get(control), per_acct.get(acct)
            if c is None or a is None:
                continue
            cr = _r(c)
            at = c.get("signal_at")
            pairs.append({
                "day": at.date().isoformat() if hasattr(at, "date") else str(at)[:10],
                "control_r": cr, "arm_r": _r(a),
                "control_deployed": c.get("deployed_risk"),
                "arm_deployed": a.get("deployed_risk"),
                "control_win": (cr is not None and cr > 0),
            })
        arms[str(acct)] = delever_null(pairs)
    return {"control_account_id": control, "arms": arms}


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
                "legs": 0, "be_legs": 0, "winners": 0, "winners_tp3": 0,
                # #188: the DEPLOYED side. `planned` is what sizing intended;
                # an arm that does not deploy its plan gets a better R for free.
                "sum_planned": 0.0, "sum_deployed": 0.0, "n_deployed": 0,
                "sum_r_deployed": 0.0, "n_r_deployed": 0}

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
        # #188: deployed risk rides alongside. NULL is EXCLUDED, never read as
        # zero — a historical row with no measurement is not a row that deployed
        # nothing, and averaging it in as 0 would understate every ratio.
        dep = t.get("deployed_risk")
        if dep is not None and prisk is not None and float(prisk) != 0:
            dep = float(dep)
            a["sum_deployed"] += dep
            a["sum_planned"] += abs(float(prisk))
            a["n_deployed"] += 1
            if dep != 0:
                a["sum_r_deployed"] += pl / abs(dep)
                a["n_r_deployed"] += 1
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
            # --- #188: deployed-risk view -------------------------------------
            # `deployed_ratio` is SUM(deployed)/SUM(planned) — the ratio of
            # TOTALS, not the mean of per-trade ratios. They disagree (0.267 vs
            # 0.74 this week) because the shortfall concentrates in the largest
            # trades, and the totals ratio is the one that governs P&L.
            "n_deployed": a["n_deployed"],
            "deployed_ratio": (round(a["sum_deployed"] / a["sum_planned"], 4)
                               if a["sum_planned"] else None),
            "avg_R_deployed": (round(a["sum_r_deployed"] / a["n_r_deployed"], 4)
                               if a["n_r_deployed"] else None),
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
    # #187: the gate and the coverage travel WITH the ladder. A caller that has
    # to run a separate recompute to find out whether these numbers are usable
    # will read them as usable. `agreement_sl` is the designated gate; the
    # coverage tail is what says whether recent signals are simply absent rather
    # than unresolved.
    out["gate"] = await excursion_gate(session, basis=basis, frm=frm, to=to)
    return out


async def excursion_gate(session, *, basis: str = "signal", frm=None, to=None) -> dict:
    """The R-ladder's validation gate and coverage, computed from the persisted
    rows (#187). No recompute — this is a read."""
    from sqlalchemy import func, select
    from ..db.models import Leg, Signal, SignalExcursion, Trade
    from .excursion_store import GATE_MIN_AGREEMENT, candle_freshness

    q = (select(SignalExcursion.signal_id, SignalExcursion.race,
                SignalExcursion.horizon_capped, SignalExcursion.same_bar_ambiguous)
         .join(Signal, Signal.id == SignalExcursion.signal_id)
         .where(SignalExcursion.basis == basis))
    if frm is not None:
        q = q.where(Signal.created_at >= frm)
    if to is not None:
        q = q.where(Signal.created_at < to)
    rows = (await session.execute(q)).all()
    by_sig = {sid: (race, capped, amb) for sid, race, capped, amb in rows}

    lrows = (await session.execute(
        select(Trade.signal_id, Leg.outcome, Leg.sl_moved)
        .join(Leg, Leg.trade_id == Trade.id)
        .where(Trade.signal_id.in_(list(by_sig) or [-1])))).all()
    stopped, ratcheted, took_tp = set(), set(), set()
    for sid, outcome, moved in lrows:
        if outcome == "sl_hit":
            stopped.add(sid)
            if moved:
                ratcheted.add(sid)
        elif outcome == "tp_hit":
            took_tp.add(sid)
    cohort = stopped - ratcheted - took_tp
    agreed = sum(1 for sid in cohort if by_sig.get(sid, (None,))[0] == "sl")
    n = len(cohort)
    rate = round(agreed / n, 4) if n else None

    cov = (await session.execute(
        select(func.min(SignalExcursion.entry_at), func.max(SignalExcursion.entry_at),
               func.max(SignalExcursion.computed_at))
        .where(SignalExcursion.basis == basis))).one()
    first, last, computed = cov
    newer = int((await session.execute(
        select(func.count()).select_from(Signal)
        .where(Signal.created_at > last))).scalar() or 0) if last else 0

    return {
        "agreement_sl": {
            "n": n, "rate": rate, "threshold": GATE_MIN_AGREEMENT,
            "passed": (rate is not None and rate >= GATE_MIN_AGREEMENT),
            "n_excluded_ratcheted": len(stopped & ratcheted),
            "n_excluded_took_tp": len((stopped - ratcheted) & took_tp),
            "cohort": ("stopped on the ORIGINAL stop and never reached TP1 — the "
                       "only cohort where 'raced to the stop' and 'stopped out' "
                       "are the same claim (#187)"),
        },
        "coverage": {
            "first": first.isoformat() if first else None,
            "last": last.isoformat() if last else None,
            "last_recompute": computed.isoformat() if computed else None,
            # The tail lag, stated rather than left to be discovered. Signals
            # newer than the last ladder row have NO row — absent, not
            # unresolved — and a reader who cannot see that will read the ladder
            # as covering the present.
            "n_signals_after_last_row": newer,
            "note": ("Signals newer than `last` have no ladder row at all. The "
                     "usual cause is the candle store ending before them (#190), "
                     "not a failed recompute."),
        },
        "horizon_capped": sum(1 for v in by_sig.values() if v[1]),
        "same_bar_ambiguous": sum(1 for v in by_sig.values() if v[2]),
        "n_rows": len(by_sig),
        # #190: the usual cause of a lagging tail is the candle store, not the
        # recompute. Reported here so the two are never confused again.
        "candles": await candle_freshness(session),
    }


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


# --- #188: telling selection skill from de-levering ---------------------------
# The promote bar is high because a type-I error compounds permanently into the
# control with no automatic rollback. An arm that merely risks less posts a
# better R = pl/planned_risk in a losing week, passes every robustness check in
# the manual, and would be promoted as skill. These functions make that case
# fail loudly instead.
NO_SKILL = "NO_SKILL_DEMONSTRATED"
SKILL_POSSIBLE = "OUTSIDE_DELEVER_NULL"
UNDECIDABLE = "UNDECIDABLE"

# Day-block bootstrap: resample whole DAYS, not trades. Trades inside a day share
# the session, the news and often the signal, so trade-level resampling assumes an
# independence that does not hold and returns intervals that are far too tight.
DEFAULT_BOOT = 2000
DEFAULT_SEED = 20260801


def _pct(sorted_vals, q):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


def day_block_bootstrap(values_by_day, *, n_boot=DEFAULT_BOOT, seed=DEFAULT_SEED,
                        lo=0.05, hi=0.95) -> dict:
    """Mean + percentile CI, resampling whole days with replacement.

    Deterministic by construction (seeded, sorted day keys): a ruling that moves
    when you re-run it is not a ruling. Reports `n_blocks`, because a single
    block has zero between-block variance and yields a degenerate interval — the
    exact trap that made this week's post-changeover dR unusable (#186)."""
    days = sorted(values_by_day)
    flat = [v for d in days for v in values_by_day[d]]
    if not flat:
        return {"n": 0, "n_blocks": 0, "mean": None, "ci_low": None,
                "ci_high": None, "degenerate": True}
    import random
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        picked = [values_by_day[days[rng.randrange(len(days))]] for _ in days]
        vals = [v for blk in picked for v in blk]
        if vals:
            means.append(sum(vals) / len(vals))
    means.sort()
    return {
        "n": len(flat), "n_blocks": len(days),
        "mean": round(sum(flat) / len(flat), 4),
        "ci_low": round(_pct(means, lo), 4) if means else None,
        "ci_high": round(_pct(means, hi), 4) if means else None,
        # One block => zero between-block variance => the interval collapses to a
        # point and means nothing. Say so rather than reporting [x, x] as tight.
        "degenerate": len(days) < 2,
    }


def delever_null(pairs, *, n_boot=DEFAULT_BOOT, seed=DEFAULT_SEED) -> dict:
    """Is an arm's paired dR explained by it simply risking less? (#188)

    `pairs`: dicts {day, control_r, arm_r, control_deployed, arm_deployed,
    control_win}. One per signal traded by BOTH arms — an unmatched signal says
    nothing about the difference between them.

    THE NULL. Take the control's own P&L, scale it by the measured
    `deployed_ratio`, and call that the arm. That is a pure de-lever with zero
    selection skill: R is linear in P&L, so the null arm's R is just
    `control_r * ratio`. Bootstrap the paired dR of that fiction. If the arm's
    OBSERVED dR lands inside the band, a strategy with no skill whatsoever
    reproduces its result and the arm has demonstrated nothing.

    CAPTURE is the second, independent test. If staging really were selecting,
    it would deploy less on losers than on winners — an ASYMMETRY. Symmetric
    capture means it is just a smaller version of the control."""
    pairs = [p for p in pairs if p.get("control_r") is not None
             and p.get("arm_r") is not None]
    if not pairs:
        return {"n": 0, "verdict": UNDECIDABLE,
                "reason": "no matched pairs with R on both arms"}

    c_dep = sum(float(p.get("control_deployed") or 0) for p in pairs)
    a_dep = sum(float(p.get("arm_deployed") or 0) for p in pairs)
    ratio = (a_dep / c_dep) if c_dep else None

    def _cap(want_win):
        cs = sum(float(p.get("control_deployed") or 0) for p in pairs
                 if bool(p.get("control_win")) is want_win)
        as_ = sum(float(p.get("arm_deployed") or 0) for p in pairs
                  if bool(p.get("control_win")) is want_win)
        n = sum(1 for p in pairs if bool(p.get("control_win")) is want_win)
        return (round(as_ / cs, 4) if cs else None), n

    win_capture, n_win = _cap(True)
    loss_capture, n_loss = _cap(False)
    asym = (round(win_capture - loss_capture, 4)
            if (win_capture is not None and loss_capture is not None) else None)

    obs_by_day, null_by_day = {}, {}
    for p in pairs:
        d = str(p.get("day") or "")
        obs_by_day.setdefault(d, []).append(float(p["arm_r"]) - float(p["control_r"]))
        if ratio is not None:
            null_by_day.setdefault(d, []).append(
                float(p["control_r"]) * ratio - float(p["control_r"]))
    observed = day_block_bootstrap(obs_by_day, n_boot=n_boot, seed=seed)
    null = day_block_bootstrap(null_by_day, n_boot=n_boot, seed=seed)

    verdict, reason = UNDECIDABLE, "deployed risk not measured on both arms"
    if ratio is not None and observed["mean"] is not None and null["ci_low"] is not None:
        inside = null["ci_low"] <= observed["mean"] <= null["ci_high"]
        if observed["degenerate"] or null["degenerate"]:
            verdict = UNDECIDABLE
            reason = ("single day-block: the bootstrap has zero between-block "
                      "variance and its interval is a point, not a range")
        elif inside:
            verdict = NO_SKILL
            reason = (f"observed dR {observed['mean']} lies inside the de-lever "
                      f"null [{null['ci_low']}, {null['ci_high']}] — scaling the "
                      f"control's own P&L by {round(ratio, 4)} reproduces it with "
                      "zero selection skill")
        else:
            verdict = SKILL_POSSIBLE
            reason = (f"observed dR {observed['mean']} lies outside the de-lever "
                      f"null [{null['ci_low']}, {null['ci_high']}]")

    return {
        "n": len(pairs), "deployed_ratio": round(ratio, 4) if ratio else None,
        "win_capture": win_capture, "n_win": n_win,
        "loss_capture": loss_capture, "n_loss": n_loss,
        "capture_asymmetry": asym,
        "observed_dR": observed, "delever_null_dR": null,
        "verdict": verdict, "reason": reason,
        "note": ("An arm inside its own de-lever null has NOT demonstrated "
                 "selection skill, however robust its dR looks: leave-one-out "
                 "and a CI excluding zero are both satisfied by simply risking "
                 "less. Symmetric capture (win ~ loss) is the corroborating "
                 "sign — a selecting arm would deploy less on losers."),
    }


# --- #186: ruling a FILTER arm on what it removed -----------------------------
# Arm C's false positive had a twin on Arm B, and it is the opposite error: the
# `adx_regime` filter ran for four days without firing once, which makes Arm B a
# literal duplicate of the control. "No difference" then reads as "the filter has
# no edge" when it is really "the filter was never tested". Both verdicts are
# reported here by name so they cannot be confused for each other.
def _day_key(t) -> "str | None":
    """The UTC day a control trade belongs to, for the day blocks. Reads an
    explicit `day` first, then the timestamps a trade row carries; a row with no
    date at all simply cannot join a block, and is dropped rather than pooled
    into a fake one."""
    for key in ("day", "closed_at", "opened_at", "created_at"):
        v = t.get(key)
        if v in (None, ""):
            continue
        if hasattr(v, "date"):
            return str(v.date())
        return str(v)[:10]
    return None


def _channel_key(t):
    """Which signal source a control trade came from, for the LOCO family."""
    for key in ("source_id", "channel", "source"):
        v = t.get(key)
        if v not in (None, ""):
            return str(v)
    return None


def day_block_bootstrap_diff(rows_by_day, *, n_boot=DEFAULT_BOOT,
                             seed=DEFAULT_SEED, lo=0.05, hi=0.95) -> dict:
    """Day-block bootstrap of a DIFFERENCE of two pooled means (#212).

    `rows_by_day`: {day: [(value, in_group_a), ...]}. Each resample draws whole
    days with replacement and recomputes `mean(A) - mean(B)` over everything it
    drew, so the two groups move together within a day — which is the point: the
    removed and kept sets share a day's regime, and resampling them independently
    would break the very correlation the day block exists to preserve.

    `day_block_bootstrap` cannot express this: a difference of two means is not
    the mean of any per-observation value, so it needs its own resampler rather
    than a clever re-encoding of the inputs. Same seed, same determinism
    contract, same `n_blocks` / `degenerate` reporting — a one-block interval has
    zero between-block variance and must never be read as tight."""
    days = sorted(rows_by_day)
    flat = [r for d in days for r in rows_by_day[d]]
    n_a = sum(1 for _v, a in flat if a)
    n_b = len(flat) - n_a
    empty = {"n": len(flat), "n_a": n_a, "n_b": n_b, "n_blocks": len(days),
             "mean": None, "ci_low": None, "ci_high": None, "degenerate": True}
    if not n_a or not n_b:
        return empty                              # one side is empty: no contrast

    def _diff(rows):
        a = [v for v, grp in rows if grp]
        b = [v for v, grp in rows if not grp]
        if not a or not b:
            return None
        return sum(a) / len(a) - sum(b) / len(b)

    point = _diff(flat)
    import random
    rng = random.Random(seed)
    diffs = []
    for _ in range(n_boot):
        picked = [rows_by_day[days[rng.randrange(len(days))]] for _ in days]
        d = _diff([r for blk in picked for r in blk])
        if d is not None:
            diffs.append(d)
    diffs.sort()
    return {
        "n": len(flat), "n_a": n_a, "n_b": n_b, "n_blocks": len(days),
        "mean": round(point, 4),
        "ci_low": round(_pct(diffs, lo), 4) if diffs else None,
        "ci_high": round(_pct(diffs, hi), 4) if diffs else None,
        "degenerate": len(days) < 2,
    }


def _excludes_zero(block) -> bool:
    lo_, hi_ = block.get("ci_low"), block.get("ci_high")
    if lo_ is None or hi_ is None or block.get("degenerate"):
        return False
    return lo_ > 0 or hi_ < 0


def _leave_one_out_family(rows, key, *, n_boot, seed) -> "dict | None":
    """Recompute the dR with each distinct `key` value dropped in turn.

    Returns sign-stability COUNTS rather than one interval, which is the whole
    point: Arm C's mined suite read -0.2088 with a clean interval and collapsed
    to -0.0044 when a single channel was dropped. An effect carried by one
    channel or one day is not an effect, and only the family shows that."""
    groups = sorted({r[key] for r in rows if r.get(key) is not None},
                    key=str)
    if len(groups) < 2:
        return None
    folds, same_sign, excl = 0, 0, 0
    point = _diff_of(rows)
    if point is None or point == 0:
        return None
    for g in groups:
        kept = [r for r in rows if r[key] != g]
        blk = _dr_of(kept, n_boot=n_boot, seed=seed)
        if blk["mean"] is None:
            continue
        folds += 1
        # A fold that lands exactly on zero is NOT the same sign as a negative
        # pooled effect: `0 > 0` is False and so is `point > 0`, which would
        # score a collapsed fold as stable.
        if blk["mean"] and (blk["mean"] > 0) == (point > 0):
            same_sign += 1
        if _excludes_zero(blk):
            excl += 1
    if not folds:
        return None
    return {"folds": folds, "same_sign": same_sign, "excludes_zero": excl,
            "dropped": [str(g) for g in groups]}


def _diff_of(rows) -> "float | None":
    a = [r["r"] for r in rows if r["removed"]]
    b = [r["r"] for r in rows if not r["removed"]]
    if not a or not b:
        return None
    return sum(a) / len(a) - sum(b) / len(b)


def _dr_of(rows, *, n_boot, seed) -> dict:
    by_day: dict = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append((r["r"], r["removed"]))
    return day_block_bootstrap_diff(by_day, n_boot=n_boot, seed=seed)


def removed_vs_kept_expectancy(rows, *, n_boot=DEFAULT_BOOT,
                               seed=DEFAULT_SEED) -> dict:
    """The removed-vs-kept mean-R difference on the control, with both
    leave-one-out families (#212).

    `rows`: [{r, day, channel, removed}] — one per scored control trade in the
    epoch's window, `removed=True` for the ones the filter skipped. NEGATIVE dR
    means the removed set was worse than what was kept, i.e. the filter is
    cutting losers.

    WHY THIS EXISTS. `filter_removed_set` ruled on a win-rate bound, and on this
    book that arm can essentially never fire: at `payoff_ratio 0.427` a bucket
    losing -0.21R per trade still wins ~55% of the time, so the posterior upper
    bound sits ABOVE the base rate while the money screams. The estimator was
    specified for a book whose losses come from FREQUENCY; this book's losses
    come from MAGNITUDE."""
    rows = [r for r in rows or ()
            if r.get("r") is not None and r.get("day") is not None]
    blk = _dr_of(rows, n_boot=n_boot, seed=seed)
    return {
        "dR": blk["mean"], "ci": (blk["ci_low"], blk["ci_high"]),
        "n_blocks": blk["n_blocks"], "degenerate": blk["degenerate"],
        "n_removed": blk["n_a"], "n_kept": blk["n_b"],
        "excludes_zero": _excludes_zero(blk),
        "loco": _leave_one_out_family(rows, "channel", n_boot=n_boot, seed=seed),
        "lodo": _leave_one_out_family(rows, "day", n_boot=n_boot, seed=seed),
    }


def _expectancy_verdict(exp) -> "str | None":
    """REMOVES_LOSERS / REMOVES_WINNERS on the expectancy arm, or None.

    Deliberately strict, because this criterion is the one that can fire on this
    book: the interval must exclude zero over at least two day blocks AND every
    leave-one-channel-out and leave-one-day-out fold must keep the sign. One
    channel carrying the whole effect is exactly how #215's mined suite passed a
    week before failing replication."""
    if exp is None or exp["dR"] is None or not exp["excludes_zero"]:
        return None
    for fam in (exp["loco"], exp["lodo"]):
        if not fam or fam["folds"] < 2 or fam["same_sign"] != fam["folds"]:
            return None
    return FILTER_REMOVES_LOSERS if exp["dR"] < 0 else FILTER_REMOVES_WINNERS


FILTER_UNTESTED = "UNTESTED"
FILTER_ACCUMULATE = "ACCUMULATE"
FILTER_REMOVES_LOSERS = "REMOVES_LOSERS"
FILTER_REMOVES_WINNERS = "REMOVES_WINNERS"
FILTER_NO_EVIDENCE = "NO_EVIDENCE"

# CLAUDE.md §2.4 / min_trades_for_significance. Effective-N is well below raw-N
# here (one instrument, clustered channels), so this is a floor, not a target.
MIN_REMOVED_N = 30


def filter_removed_set(skips, control_trades, *, base_rate,
                       min_n: int = MIN_REMOVED_N, cred: float = 0.90,
                       eps: float = 0.0, n_boot: int = DEFAULT_BOOT,
                       seed: int = DEFAULT_SEED) -> dict:
    """Score a filter arm by what it REMOVED, accumulated and tested ONCE (#186).

    A filter arm trades fewer signals, so its own gross is smaller by
    construction and comparing it is meaningless. The only question that means
    anything is: **the signals it skipped — what did those same signals do on the
    control?** Lost money there, the filter works; made money there, it is
    cutting profit rather than stops.

    `skips`: one row per skip, `{signal_id, epoch}` — from
    `events.kind='entry_filtered'` with `payload.reason='filtration_skip'` on the
    filter account. Deduped by signal within an epoch, because one signal fans
    out to several legs and each can log its own event.
    `control_trades`: `{signal_id, realized_pl, planned_risk}` for the CONTROL
    arm. Broker-truth P&L, never `legs.realized_pl` (CLAUDE.md §2.5).

    TWO DISCIPLINES ARE STRUCTURAL HERE, not left to the caller's memory.

    **Epochs are never pooled.** `epoch` names the filter's configuration — its
    rule and its timeframe, e.g. `adx_regime@4h`. Do NOT hand-write it: since
    #200 it is `analysis.epochs.epoch_name(entry_filters, entry_policy)`, and it
    is persisted on the strategy row (`epoch_digest` / `epoch_started_at`, and
    the `epoch` field the strategies API returns). A literal typed into a weekly
    script has to be re-derived from `updated_at` every time the config moves,
    which is exactly how a week of skips gets assigned to a filter that was not
    running. Moving the `adx_regime` filter
    from 4h to 1h mid-week made it a different experiment: the 4h half fired ZERO
    times (Arm B ≡ control) and the 1h half fired 8. Averaging the two describes
    no filter that ever ran. So the return is per epoch, and there is deliberately
    no pooled-across-epochs figure to reach for.

    **One test, at accumulated N.** Pass every week's skips for an epoch, not one
    week's: re-reading a fresh 90% CI each week is repeated peeking and inflates
    type-I exactly where the promote bar is supposed to be high. Below `min_n`
    the verdict is `ACCUMULATE` and NO interval is offered to act on — the number
    is still returned, because hiding it would just get it recomputed by hand.

    Read the win rate on the NEAR bound: this is a bucket you want to EXCLUDE, so
    the bound that matters is the UPPER one. And the P&L sign must AGREE with it
    before either becomes a verdict — TP1 distance varies ~7x across channels, so
    a removed set can be low-win-rate and net-POSITIVE, and a filter that removes
    money is not helping however its win rate reads.

    **TWO CO-EQUAL CRITERIA (#212).** The win-rate arm above cannot fire on this
    book. At `payoff_ratio 0.427` (avg win +0.31R, avg loss -0.73R) a removed set
    losing -0.21R per trade still wins ~55% of the time, so its posterior upper
    bound lands ABOVE the base rate while the money screams — the estimator was
    specified for losses that come from FREQUENCY, and these come from MAGNITUDE.
    Both live epochs landed in exactly that hole, at N well above the floor, on
    the first genuinely frozen window the experiment has ever produced.

    So the arm rules `REMOVES_LOSERS` when EITHER the win-rate-and-sign test
    fires, OR the removed-vs-kept mean-R difference clears a strictly harder bar:
    a day-block CI excluding zero over at least two blocks, sign-stable across
    EVERY leave-one-channel-out and leave-one-day-out fold. `criterion` says
    which one spoke. When the two point opposite ways the verdict stays
    `NO_EVIDENCE` — two criteria disagreeing is not a promotion.

    The expectancy arm needs `day` (and, for LOCO, `source_id`/`channel`) on each
    control trade. Without them it is simply absent and the win-rate arm rules
    alone, exactly as before — an epoch is never ruled on a statistic that could
    not be computed. `control_trades` must cover the epoch's window: everything
    in it that is not in the removed set IS the kept set."""
    by_sig = {}
    for t in control_trades or ():
        sig = t.get("signal_id")
        if sig is not None:
            by_sig[sig] = t

    epochs: dict = {}
    for s in skips or ():
        sig = s.get("signal_id")
        if sig is None:
            continue
        epochs.setdefault(str(s.get("epoch") or "?"), set()).add(sig)

    out: dict = {}
    for epoch, sigs in sorted(epochs.items()):
        n_skipped = len(sigs)
        pls, rs = [], []
        n_unscoreable = 0
        for sig in sorted(sigs, key=str):
            t = by_sig.get(sig)
            pl = None if t is None else t.get("realized_pl")
            if pl is None:
                # The control never closed a trade on this signal either — its own
                # risk guard, breaker or fill failure took it. It is NOT a
                # zero-P&L removal; it is a signal the removed set cannot score,
                # and counting it as flat would drag the mean toward nothing.
                n_unscoreable += 1
                continue
            pl = float(pl)
            pls.append(pl)
            pr = t.get("planned_risk")
            if pr not in (None, 0) and float(pr) != 0:
                rs.append(pl / abs(float(pr)))

        # The expectancy arm compares the removed set against what was KEPT, so
        # it reads every scored control trade in the window, not only the skips.
        exp_rows = []
        for _sig, t in by_sig.items():
            _pl, _pr = t.get("realized_pl"), t.get("planned_risk")
            if _pl is None or _pr in (None, 0) or float(_pr) == 0:
                continue
            _day = _day_key(t)
            if _day is None:
                continue
            exp_rows.append({"r": float(_pl) / abs(float(_pr)), "day": _day,
                             "channel": _channel_key(t),
                             "removed": _sig in sigs})
        exp = (removed_vs_kept_expectancy(exp_rows, n_boot=n_boot, seed=seed)
               if exp_rows else None)
        if exp is not None and (not exp["n_removed"] or not exp["n_kept"]):
            exp = None                            # one side empty: no contrast

        n_scored = len(pls)
        wins = sum(1 for p in pls if p > eps)
        n_flat = sum(1 for p in pls if abs(p) <= eps)
        n_decisive = n_scored - n_flat
        net = round(sum(pls), 2) if pls else 0.0
        post = (posterior(wins, n_decisive, base_rate, cred=cred)
                if n_decisive else None)
        ci = ((round(post["ci_low"], 4), round(post["ci_high"], 4))
              if post else (None, None))

        wr_verdict = None
        if ci[1] is not None and ci[1] < base_rate and net < 0:
            wr_verdict = FILTER_REMOVES_LOSERS
        elif ci[0] is not None and ci[0] > base_rate and net > 0:
            wr_verdict = FILTER_REMOVES_WINNERS
        exp_verdict = _expectancy_verdict(exp)
        criterion = None

        if not n_skipped:
            verdict, reason = FILTER_UNTESTED, "the filter never fired"
        elif n_decisive < min_n:
            verdict = FILTER_ACCUMULATE
            reason = (f"{n_decisive} decisive removals of the {min_n} needed. "
                      "Accumulate the NEXT weeks into this same epoch and test "
                      "once — do not read the interval yet")
        elif wr_verdict and exp_verdict and wr_verdict != exp_verdict:
            verdict = FILTER_NO_EVIDENCE
            reason = (f"the two criteria disagree: the win rate reads "
                      f"{wr_verdict}, the removed-vs-kept expectancy "
                      f"({exp['dR']}R, CI {exp['ci']}) reads {exp_verdict}. "
                      "Two criteria pointing opposite ways is not a promotion")
        elif wr_verdict == FILTER_REMOVES_LOSERS:
            verdict, criterion = wr_verdict, "win_rate"
            reason = (f"the removed set won {ci[0]}-{ci[1]} against a base of "
                      f"{round(base_rate, 4)} and cost the control {net} — the "
                      "upper bound is below the base, so the filter is cutting "
                      "losers on the bound that matters for an exclusion")
        elif wr_verdict == FILTER_REMOVES_WINNERS:
            verdict, criterion = wr_verdict, "win_rate"
            reason = (f"the removed set won {ci[0]}-{ci[1]} against a base of "
                      f"{round(base_rate, 4)} and MADE the control {net} — the "
                      "filter is cutting profit, not stops")
        elif exp_verdict:
            verdict, criterion = exp_verdict, "expectancy"
            _worse = "worse" if exp["dR"] < 0 else "better"
            reason = (f"the win rate cannot separate on this payoff geometry, "
                      f"but the removed set ran {exp['dR']}R {_worse} than what "
                      f"was kept, CI {exp['ci']} over {exp['n_blocks']} day "
                      f"blocks, sign-stable on every leave-one-channel-out "
                      f"({exp['loco']['same_sign']}/{exp['loco']['folds']}) and "
                      f"leave-one-day-out ({exp['lodo']['same_sign']}/"
                      f"{exp['lodo']['folds']}) fold")
        elif ci[0] is not None and (ci[1] < base_rate or ci[0] > base_rate):
            verdict = FILTER_NO_EVIDENCE
            reason = (f"the win rate and the P&L disagree: interval "
                      f"[{ci[0]}, {ci[1]}] vs base {round(base_rate, 4)}, net "
                      f"{net}. A removed set can be low-win-rate and net-positive "
                      "when its TP1 sits close — neither alone is a verdict")
        else:
            verdict = FILTER_NO_EVIDENCE
            reason = (f"interval [{ci[0]}, {ci[1]}] spans the base rate "
                      f"{round(base_rate, 4)} — no evidence of edge either way")

        out[epoch] = {
            "n_skipped": n_skipped, "n_scored": n_scored,
            "n_unscoreable": n_unscoreable,
            "n_decisive": n_decisive, "n_flat": n_flat, "wins": wins,
            "removed_set_net": net,
            "win_rate_ci": ci, "base_rate": round(base_rate, 4),
            "mean_r": round(sum(rs) / len(rs), 4) if rs else None,
            "sum_r": round(sum(rs), 4) if rs else None,
            "verdict": verdict, "reason": reason, "criterion": criterion,
            # The expectancy arm is reported whenever it could be computed, even
            # when it rules nothing: a number that is hidden this week is a
            # number recomputed by hand next week, which is what #186 exists to
            # end. `n_blocks` rides with it because a one-block interval has no
            # between-block variance and must never be read as tight.
            "expectancy_dR": None if exp is None else exp["dR"],
            "expectancy_ci": (None, None) if exp is None else exp["ci"],
            "n_blocks": 0 if exp is None else exp["n_blocks"],
            "loco_sign_stable": None if exp is None else exp["loco"],
            "lodo_sign_stable": None if exp is None else exp["lodo"],
        }

    return {
        "epochs": out, "n_epochs": len(out), "min_n": min_n,
        "note": ("Per EPOCH, never pooled across one: a filter that changed its "
                 "timeframe is a different experiment, and a half that never "
                 "fired is UNTESTED, not null. Accumulate weeks WITHIN an epoch "
                 "and test once — a fresh interval each week is peeking."),
    }


async def stop_geometry_report(session, frm=None, to=None,
                               floor: float = None) -> dict:
    """Per-signal sub-ATR stop labels + the widen-and-resize counterfactual (#189).

    Joins three persisted layers, none of which is recomputed here:
      * `signals`            the geometry as called (entry, sl, tp1)
      * `signal_analytics`   the regime block, for `atr_pct` and `price`
      * `signal_excursions`  the exit-INDEPENDENT travel (mfe_r / mae_r), which
                             is the only honest way to ask whether a wider stop
                             would have survived

    SHADOW. Read-only, gates nothing, and the trading path never consults it."""
    from sqlalchemy import select
    from ..db.models import Signal, SignalAnalytics, SignalExcursion, Trade, Leg
    from .stop_geometry import DEFAULT_FLOOR, rollup, shadow_label

    floor = DEFAULT_FLOOR if floor is None else float(floor)
    q = (select(Signal.id, Signal.entry_from, Signal.sl, Signal.tps,
                SignalAnalytics.price, SignalAnalytics.analytics,
                SignalExcursion.mfe_r, SignalExcursion.mae_r)
         .select_from(Signal)
         .join(SignalAnalytics, SignalAnalytics.signal_id == Signal.id)
         .outerjoin(SignalExcursion,
                    (SignalExcursion.signal_id == Signal.id)
                    & (SignalExcursion.basis == "signal")))
    if frm is not None:
        q = q.where(Signal.created_at >= frm)
    if to is not None:
        q = q.where(Signal.created_at < to)
    rows = (await session.execute(q)).all()

    sids = [r[0] for r in rows]
    actual = {}
    if sids:
        for sid, pl, risk in (await session.execute(
                select(Trade.signal_id, Trade.realized_pl, Trade.planned_risk)
                .where(Trade.signal_id.in_(sids),
                       Trade.status == "closed"))).all():
            if pl is not None and risk not in (None, 0) and float(risk) != 0:
                actual.setdefault(sid, float(pl) / abs(float(risk)))

    labels = []
    for sid, entry, sl, tps, price, an, mfe, mae in rows:
        regime = ((an or {}).get("regime") or {})
        tp1 = (tps or [None])[0]
        lab = shadow_label(entry=entry, sl=sl, atr_pct=regime.get("atr_pct"),
                           price=price, mfe_r=mfe, mae_r=mae, tp1=tp1,
                           floor=floor)
        if lab is None:
            continue
        lab["signal_id"] = sid
        if sid in actual:
            lab["actual_r"] = round(actual[sid], 4)
        labels.append(lab)
    out = rollup(labels)
    out["floor"] = floor
    out["labels"] = labels
    return out
