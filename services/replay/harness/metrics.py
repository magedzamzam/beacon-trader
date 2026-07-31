"""Result metrics — the SAME keys as a live weekly, plus the caveats a
simulation owes its reader (#169 §4, §8).

`geometry_ab_rollup` is imported from `beacon_core.analysis.report` and fed
simulated trades/legs in the shape it already takes. Not re-derived, not
re-rounded, not "compatible": the same function, so an offline result drops into
the same report template and is directly comparable to a live arm. If the live
definition of `payoff_ratio` changes, the harness changes with it for free.

WHAT THE ROLLUP DOES NOT SEE, AND WHY IT IS REPORTED BESIDE IT
-------------------------------------------------------------
A trade that never filled is EXCLUDED from the rollup rather than entered as a
zero. A never-filled entry is a trade that did not happen; counting it as a
0-P&L loss would drag every variant's win rate toward the share of orders that
expired, which is an execution statistic wearing a prediction statistic's
clothes. The count is reported as `n_never_filled`.

Likewise `same_bar_ambiguous`, `horizon_capped`, `suspect_excluded` and the
blocked-by-caps counts are headline fields, not footnotes: each one is a
quantity of "we do not actually know", and a reader who cannot see them cannot
judge the result.

OVERFITTING GUARDRAILS (§8) are enforced here, not merely documented:
  * `held_out` is computed and is the HEADLINE; in-sample is labelled as such.
  * `n_variants_searched` rides on every report — best-of-N is upward-biased by
    construction and a winner shown without N is misleading.
  * `verdict_withheld` fires below the live N>=30 floor.
  * regime composition of the window is reported, so "robust" cannot be claimed
    beyond the regimes actually tested.

PURE — stdlib + beacon_core.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import Counter, defaultdict
from decimal import Decimal
from typing import Iterable, List, Optional, Sequence

from beacon_core.analysis.report import geometry_ab_rollup
from beacon_core.ta.indicators import adx as _adx

from . import bars as B
from . import fills as F
from .portfolio import VariantResult

# The live significance floor (CLAUDE.md §2.4). Replay does not get a lower bar
# just because it is cheap to generate trades.
MIN_TRADES_FOR_VERDICT = 30

# Rolling-ADX window for the regime split. 4h is the timeframe the entry filters
# already reason about, so the composition is stated in the same units an
# operator would gate on.
REGIME_TIMEFRAME = "4h"
REGIME_PERIOD = 14
REGIME_WINDOW = 100


def _rows(trades: Iterable, accounts_by_id: dict) -> tuple:
    """(trade dicts, leg dicts) in the shape `geometry_ab_rollup` consumes."""
    t_rows, l_rows = [], []
    for i, t in enumerate(trades):
        if not t.ever_filled:
            continue
        tid = f"{t.signal_id}:{t.account_id}:{i}"
        t_rows.append({
            "trade_id": tid, "account_id": t.account_id,
            "account": accounts_by_id.get(t.account_id, ""),
            "realized_pl": float(t.realized_pl),
            "planned_risk": float(t.planned_risk) if t.planned_risk else None,
            "strategy_label": t.strategy_label,
        })
        for leg in t.legs:
            if leg.outcome is None:
                continue
            l_rows.append({"trade_id": tid, "outcome": leg.outcome,
                           "tp_index": leg.tp_index})
    return t_rows, l_rows


def rollup(trades: Sequence, *, accounts_by_id: dict,
           source_id=None) -> dict:
    t_rows, l_rows = _rows(trades, accounts_by_id)
    return geometry_ab_rollup(t_rows, l_rows, source_id=source_id)


def returns_by_arm(trades: Sequence, accounts) -> dict:
    """Period return as a PERCENTAGE of starting equity, per account.

    R-multiples stay the primary comparator — scale-free, and they dissolve the
    equity-parity confound between arms — but "what would this have made" is the
    question an operator actually asks, and answering only in R invites the
    reader to do the conversion in their head, badly.

    Equity is held CONSTANT across a run (see `variants.DEFAULT_EQUITY`), so
    this is simple return on starting capital with no compounding. It is a
    PERIOD return over whatever window the run covered and is deliberately NOT
    annualised: scaling four weeks of one instrument up to a year turns a small
    sample into a confident-looking number, which is the exact failure §8
    exists to prevent."""
    equity = {a.id: float(a.equity or 0) for a in accounts}
    net: dict = {}
    n: dict = {}
    for t in trades:
        if not t.ever_filled:
            continue
        net[t.account_id] = net.get(t.account_id, 0.0) + float(t.realized_pl)
        n[t.account_id] = n.get(t.account_id, 0) + 1
    out, total_net, total_eq = {}, 0.0, 0.0
    for acct_id in sorted(net, key=lambda a: (a is None, a)):
        pl = net[acct_id]
        eq = equity.get(acct_id) or 0.0
        out[str(acct_id)] = {
            "account_id": acct_id, "n_trades": n.get(acct_id, 0),
            "starting_equity": eq, "net_nominal": round(pl, 2),
            "return_pct": round(100.0 * pl / eq, 4) if eq else None}
        total_net += pl
        total_eq += eq
    return {
        "by_account": out,
        "total_net_nominal": round(total_net, 2),
        "total_return_pct": (round(100.0 * total_net / total_eq, 4)
                             if total_eq else None),
        "note": ("Period return on STARTING equity — not compounded, and NOT "
                 "annualised. Rank on R (scale-free); quote this. Excludes "
                 "trades that never filled: an entry that did not fill is not a "
                 "0% trade."),
    }


def _split(trades: Sequence, holdout_from: Optional[dt.datetime]) -> tuple:
    """(in_sample, held_out) by signal time. With no split date everything is
    in-sample and the report says so — the harness never silently reports an
    in-sample number as if it were out-of-sample."""
    if holdout_from is None:
        return list(trades), []
    ins, out = [], []
    for t in trades:
        (out if t.signal_at >= holdout_from else ins).append(t)
    return ins, out


def regime_composition(series: B.BarSeries, *, frm=None, to=None,
                       timeframe: str = REGIME_TIMEFRAME) -> dict:
    """Share of the test window that was ADX-trending vs ranging.

    ~7 months of 2026 gold is one or two regimes. A variant tuned on it has not
    been shown to generalise, and the only honest way to say so is to state what
    the window actually contained (§8.4)."""
    frame = B.resample(series.bars, timeframe)
    if frm is not None:
        frame = [b for b in frame if b.ts >= frm]
    if to is not None:
        frame = [b for b in frame if b.ts < to]
    need = 2 * REGIME_PERIOD + 1
    if len(frame) < need:
        return {"timeframe": timeframe, "n_bars": len(frame),
                "trending_share": None, "note": "series too short to classify"}
    trending = 0
    scored = 0
    for i in range(need, len(frame) + 1):
        win = frame[max(0, i - REGIME_WINDOW):i]
        d = _adx([b.high for b in win], [b.low for b in win],
                 [b.close for b in win], REGIME_PERIOD)
        if not d or d.get("adx") is None:
            continue
        scored += 1
        trending += 1 if d.get("trending") else 0
    return {"timeframe": timeframe, "n_bars": len(frame), "n_scored": scored,
            "trending_share": round(trending / scored, 4) if scored else None,
            "ranging_share": round(1 - trending / scored, 4) if scored else None,
            "adx_threshold": 25,
            "note": ("Regime mix of the tested window. A result is not evidence "
                     "of robustness outside this mix.")}


def _caveats(res: VariantResult, trades: Sequence) -> dict:
    n_filled = sum(1 for t in trades if t.ever_filled)
    return {
        "n_signals_evaluated": sum(res.counts[k] for k in res.counts
                                   if k in ("taken",)) + len(res.not_taken),
        "n_taken": res.counts["taken"],
        "n_never_filled": sum(1 for t in trades if not t.ever_filled),
        "n_filled": n_filled,
        "n_blocked_by_risk_limits": res.counts["risk_limit_block"],
        "n_blocked_by_breaker": res.counts["breaker_block"],
        "n_filtered_out": res.counts["filtration_skip"],
        "n_no_candle_coverage": res.counts["no_candle_coverage"],
        "n_horizon_capped": res.counts["horizon_capped"],
        "n_same_bar_ambiguous_legs": res.counts["same_bar_ambiguous"],
        "not_taken_breakdown": dict(Counter(
            r["reason"] for r in res.not_taken)),
        "suspect_bars_excluded": res.coverage.get("suspect_excluded"),
        "note": ("Same-bar TP+SL is scored as the STOP; the count is how often "
                 "the 1m bar could not say. Horizon-capped trades were still open "
                 "when the replay window ended and are marked to market, not won. "
                 "Never-filled trades are EXCLUDED from the rollup — an entry that "
                 "never filled is not a zero-P&L trade."),
    }


def _verdict(n: int) -> dict:
    return {
        "n_closed": n,
        "min_trades_for_verdict": MIN_TRADES_FOR_VERDICT,
        "verdict_withheld": n < MIN_TRADES_FOR_VERDICT,
        "effective_n_note": ("Effective N is well below raw N: every trade is the "
                             "same instrument over overlapping windows, so the "
                             "samples are correlated. Treat raw N as an upper "
                             "bound on the evidence, never as the evidence."),
    }


def variant_report(res: VariantResult, *, variant, series: B.BarSeries,
                   holdout_from: Optional[dt.datetime] = None,
                   n_variants_searched: int = 1,
                   sources_by_id: Optional[dict] = None) -> dict:
    """The full result for one variant: pooled, per-source, and split
    in-sample / held-out — with the guardrail block attached to all of it.

    Per-source is not an optional extra. The correct exit almost certainly
    differs by channel (median TP1 ranges 0.15R for TFXC to 1.00R for Quartz,
    #182), so a pooled-only answer averages away the thing being measured."""
    accounts_by_id = {a.id: (a.name or f"acct#{a.id}") for a in variant.accounts}
    sources_by_id = sources_by_id or {}
    trades = res.trades
    ins, out = _split(trades, holdout_from)

    by_source = {}
    grouped = defaultdict(list)
    for t in trades:
        grouped[t.source_id].append(t)
    for sid, group in sorted(grouped.items(), key=lambda kv: (kv[0] is None, kv[0])):
        by_source[str(sid)] = {
            "source_id": sid,
            "source": sources_by_id.get(sid),
            "rollup": rollup(group, accounts_by_id=accounts_by_id, source_id=sid),
            **_verdict(sum(1 for t in group if t.ever_filled)),
        }

    headline_set = out if holdout_from is not None else ins
    return {
        "variant": res.variant,
        "variant_digest": variant.digest(),
        "headline_basis": "held_out" if holdout_from is not None else "in_sample",
        "headline": rollup(headline_set, accounts_by_id=accounts_by_id),
        "returns": returns_by_arm(headline_set, variant.accounts),
        "returns_pooled": returns_by_arm(trades, variant.accounts),
        "pooled": rollup(trades, accounts_by_id=accounts_by_id),
        "in_sample": rollup(ins, accounts_by_id=accounts_by_id),
        "held_out": (rollup(out, accounts_by_id=accounts_by_id)
                     if holdout_from is not None else None),
        "by_source": by_source,
        "caveats": _caveats(res, trades),
        **_verdict(sum(1 for t in headline_set if t.ever_filled)),
        "guardrails": guardrails(n_variants_searched, holdout_from, series,
                                 frm=holdout_from),
        "coverage": res.coverage,
        "settings": {"horizon_bars": variant.horizon_bars,
                     "slippage_points": variant.slippage_points,
                     "ratchet_price": variant.ratchet_price,
                     # Whether the session risk multiplier and `session_in`
                     # rules were live for this run. Stated on the RESULT, so a
                     # variant that did not model them cannot be compared to one
                     # that did without the difference being visible (#81).
                     "sessions_modelled": bool(variant.session_windows),
                     "n_session_desized": res.counts["session_desized"]},
    }


def best_of_n_inflation(n_variants: int) -> Optional[float]:
    """Rough expected upward bias of the BEST of N independent backtests, in
    standard deviations of the per-variant noise: E[max of N standard normals]
    ~= sqrt(2 ln N). Not a correction to apply — a magnitude to be embarrassed
    by. Searching 20 variants buys you ~2.4 sigma of pure luck before any skill."""
    if n_variants is None or n_variants < 2:
        return None
    return round(math.sqrt(2 * math.log(n_variants)), 3)


def guardrails(n_variants_searched: int, holdout_from, series: B.BarSeries,
               frm=None) -> dict:
    return {
        "n_variants_searched": int(n_variants_searched or 1),
        "best_of_n_inflation_sigma": best_of_n_inflation(n_variants_searched),
        "walk_forward": {
            "holdout_from": holdout_from.isoformat() if holdout_from else None,
            "enabled": holdout_from is not None,
            "note": ("A variant's headline result is its HELD-OUT performance. "
                     "With no holdout date the result is in-sample only and is "
                     "not reportable as an edge."),
        },
        "regime": regime_composition(series, frm=frm),
        "promotion": ("Replay results are HYPOTHESIS-GENERATING, not "
                      "promotion-grade. The live frozen-week A/B/C remains the "
                      "only thing that promotes a config (CLAUDE.md §2)."),
    }
