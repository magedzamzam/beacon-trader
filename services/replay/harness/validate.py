"""The validation gate: the harness must reproduce reality before it is believed
(#169 §5).

Replay the ACTUAL live configs over the ACTUAL historical signals and reconcile
against broker truth. Until that passes, a counterfactual from this harness is
an opinion with a confidence interval attached.

WHAT IS COMPARED
  fill    simulated fill price vs the leg's real `fill_price`. A stored 0 is an
          UNKNOWN fill, not a fill at zero (#159), and is excluded rather than
          scored as a 1000-point error.
  outcome simulated leg outcome vs the real leg `outcome`, on FILLED legs only.
          An order that never filled has no outcome to agree about.
  R       simulated R vs broker-truth R. Trade-level P&L only, never
          `legs.realized_pl` (CLAUDE.md §2.5 — known cross-attribution bug).

ACCEPTANCE THRESHOLDS, stated up front so the gate cannot be moved after seeing
the number:
  * outcome agreement       >= 0.90 on filled legs
  * median |delta R|        <= 0.25
  * |mean delta R| (BIAS)   <= 0.10

The bias test is the important one and it is DIRECTIONAL. Scatter is noise;
a harness that is consistently ROSIER than live is a blocking defect, because
every variant it ranks inherits that optimism and the ranking may survive it
while the level does not. A run that fails the bias test reports
`systematic_bias: "optimistic"` and `passed: false`.

PURE — stdlib only. The DB half lives in `store.py`.
"""
from __future__ import annotations

from statistics import median
from typing import Dict, Iterable, List, Optional, Sequence

# Thresholds. Named constants, not literals buried in a comparison, so moving one
# is a visible diff.
MIN_OUTCOME_AGREEMENT = 0.90
MAX_MEDIAN_ABS_DELTA_R = 0.25
MAX_ABS_MEAN_DELTA_R = 0.10

# Outcomes that mean the same thing for agreement purposes. A stop that had been
# ratcheted to entry is labelled `breakeven` live and `breakeven` here, but the
# live labeller falls back to `sl_hit` when the broker's own close reason says so
# and the price tolerance misses — so the pair is treated as agreeing on the
# MECHANISM (the stop took it) and the distinction is reported separately.
_STOP_FAMILY = frozenset({"sl_hit", "breakeven"})

# Outcomes ordered by how good they are for the account. Used ONLY to give a
# disagreement a direction: an agreement rate says how often the simulator was
# wrong, never which way, and "wrong 8% of the time" is a very different finding
# depending on whether the errors are winners it invented or winners it missed.
_FAVOURABILITY = {"tp_hit": 3, "horizon": 2, "manual": 2, "breakeven": 1,
                  "expired": 1, "sl_hit": 0}


def _rank(outcome) -> Optional[int]:
    return _FAVOURABILITY.get(str(outcome or "").lower())


def _same_outcome(sim: Optional[str], live: Optional[str]) -> Optional[bool]:
    if sim is None or live is None:
        return None
    if sim == live:
        return True
    return sim in _STOP_FAMILY and live in _STOP_FAMILY


def compare(sim_legs: Sequence[dict], live_legs: Sequence[dict]) -> dict:
    """Reconcile two leg sets keyed by (signal_id, account_id, tp_index).

    Each row: {signal_id, account_id, tp_index, fill_price, outcome}. Live rows
    may also carry `r` (trade-level R); sim rows carry `r` from the simulated
    trade. Unmatched rows on either side are counted, never dropped silently —
    a harness that simply fails to produce the trades live took would otherwise
    score 100% agreement on the handful it did.
    """
    def key(r):
        return (r.get("signal_id"), r.get("account_id"), r.get("tp_index"))

    sim_by = {key(r): r for r in sim_legs}
    live_by = {key(r): r for r in live_legs}

    matched = sorted(set(sim_by) & set(live_by), key=lambda k: tuple(
        (x is None, x) for x in k))
    only_sim = sorted(set(sim_by) - set(live_by), key=lambda k: tuple(
        (x is None, x) for x in k))
    only_live = sorted(set(live_by) - set(sim_by), key=lambda k: tuple(
        (x is None, x) for x in k))

    fill_deltas: List[float] = []
    adverse_deltas: List[float] = []
    # #185 defect A: legs the simulator never filled and live did. 34 of the 72
    # disagreements sit here, and the CAUSE is not readable from the count.
    under_fill_misses: List[float] = []
    under_fill_by_live_outcome: dict = {}
    n_under_fill = n_under_fill_ttl = 0
    outcome_n = outcome_ok = 0
    label_mismatch = 0
    confusion: dict = {}
    sim_better = sim_worse = 0
    rows: List[dict] = []
    for k in matched:
        s, lv = sim_by[k], live_by[k]
        sf, lf = _pos(s.get("fill_price")), _pos(lv.get("fill_price"))
        d_fill = d_adv = None
        if sf is not None and lf is not None:
            d_fill = sf - lf
            fill_deltas.append(d_fill)
            # Raw (sim - live) pools BUY and SELL and is therefore NOT a bias:
            # paying 0.3 more on a BUY and receiving 0.3 more on a SELL are
            # opposite outcomes that cancel in the mean, so a harness with a
            # systematic entry advantage can average to zero. Re-sign it so
            # POSITIVE always means the simulation got the WORSE fill.
            d_adv = _adverse_delta(s.get("direction") or lv.get("direction"),
                                   sf, lf)
            if d_adv is not None:
                adverse_deltas.append(d_adv)
        if sf is None and lf is not None:
            # The simulator never filled an entry live DID fill. Record how far
            # its side came short, and what live went on to do — a miss that
            # cost a winner and a miss that dodged a loser are the same defect
            # but they pull the bias term in opposite directions.
            n_under_fill += 1
            gap = _pos_or_none(s.get("closest_approach"))
            if gap is not None:
                under_fill_misses.append(gap)
            if s.get("expired_on_fillable_bar"):
                n_under_fill_ttl += 1
            lo = str(lv.get("outcome"))
            under_fill_by_live_outcome[lo] = under_fill_by_live_outcome.get(lo, 0) + 1
        agree = None
        if lf is not None:                       # only filled legs have an outcome
            agree = _same_outcome(s.get("outcome"), lv.get("outcome"))
            if agree is not None:
                outcome_n += 1
                outcome_ok += 1 if agree else 0
                if agree and s.get("outcome") != lv.get("outcome"):
                    label_mismatch += 1
                if not agree:
                    pair = (str(s.get("outcome")), str(lv.get("outcome")))
                    confusion[pair] = confusion.get(pair, 0) + 1
                    sr, lr = _rank(s.get("outcome")), _rank(lv.get("outcome"))
                    if sr is not None and lr is not None:
                        if sr > lr:
                            sim_better += 1
                        elif sr < lr:
                            sim_worse += 1
        rows.append({"signal_id": k[0], "account_id": k[1], "tp_index": k[2],
                     "sim_outcome": s.get("outcome"), "live_outcome": lv.get("outcome"),
                     "outcome_agrees": agree, "delta_fill": d_fill,
                     "delta_fill_adverse": d_adv})

    return {
        "n_matched_legs": len(matched),
        "n_only_sim": len(only_sim), "n_only_live": len(only_live),
        "outcome": {
            "n": outcome_n,
            "agreement_rate": round(outcome_ok / outcome_n, 4) if outcome_n else None,
            "n_stop_family_relabels": label_mismatch,
            # An agreement RATE says how often the simulator was wrong and never
            # which way. "Wrong 8% of the time" is a different finding depending
            # on whether those are winners it invented or winners it missed —
            # the first inflates every variant, the second understates them.
            "n_disagreements": outcome_n - outcome_ok,
            "sim_better_than_live": sim_better,
            "sim_worse_than_live": sim_worse,
            "disagreement_bias": _bias_word(sim_better, sim_worse),
            "confusion": {f"sim={k[0]} live={k[1]}": v
                          for k, v in sorted(confusion.items(),
                                             key=lambda kv: (-kv[1], kv[0]))},
        },
        "fill": _dist(fill_deltas, "delta_fill (sim - live), instrument points "
                                   "— pooled across BUY and SELL, so the MEAN "
                                   "is not a bias; read fill_adverse for that"),
        "fill_adverse": _dist(
            adverse_deltas,
            "delta_fill re-signed so POSITIVE = the simulation got the WORSE "
            "entry. A negative mean is the harness giving itself better fills "
            "than live got, which flatters every variant it ranks."),
        "under_fill": _under_fill_block(under_fill_misses, n_under_fill,
                                        n_under_fill_ttl,
                                        under_fill_by_live_outcome),
        "rows": rows,
    }


# A miss this small is inside the spread — the level WAS reached at the broker
# and the feed's wider quote is what stopped the simulated side getting there.
SPREAD_SCALE_POINTS = 0.5


def _under_fill_block(misses: List[float], n: int, n_ttl: int,
                      by_live_outcome: dict) -> dict:
    """Name the cause of `sim=expired, live=filled` instead of guessing it (#185).

    Three candidates, and they have three different fixes:

      * THE FEED — a candle source whose spread prints wider than Capital.com's
        was. The simulated ask never reaches a level the real ask did, so the
        order expires on TTL. Signature: misses clustered at a fraction of a
        point, i.e. inside a spread.
      * THE TTL / THE WINDOW — the resolved entry TTL differing from what live
        used, or the replay window clipping the order's life. Signature: large
        misses, or none at all where the sim simply ran out of bars.
      * WITHIN-BAR ORDERING — `_expire_working` runs before `_fill_working`, so
        an order whose TTL lapsed on a bar it would have filled in is lost.
        Isolated exactly, as `n_expired_on_a_fillable_bar`; no inference needed.

    The verdict here is a POINTER, not a conclusion. It says which hypothesis the
    distribution favours so the next hour is spent on the right one — the
    feed-provenance diff against Capital.com bars is still the thing that settles
    the first candidate, and this cannot do it."""
    misses = sorted(misses)
    m = len(misses)
    med = median(misses) if m else None
    near = sum(1 for x in misses if x <= SPREAD_SCALE_POINTS)
    if n == 0:
        verdict = "none — the simulator filled every entry live filled"
    elif n_ttl and n_ttl >= 0.5 * n:
        verdict = ("within-bar ordering: most of these were retired by the TTL "
                   "on a bar they would have filled in")
    elif m and near >= 0.5 * m:
        verdict = (f"feed-shaped: {near}/{m} missed by <= {SPREAD_SCALE_POINTS} "
                   "points, which is inside a spread — diff the candle source "
                   "against Capital.com bars on high/low before anything else")
    elif m:
        verdict = ("not spread-shaped: the misses are large, so look at the "
                   "resolved entry TTL and the replay window first")
    else:
        verdict = ("no distance recorded — these entries were MARKET orders or "
                   "were never placed, not levels the market failed to reach")
    return {
        "n_sim_never_filled_live_did": n,
        "n_with_a_recorded_miss": m,
        "median_miss_points": round(med, 5) if med is not None else None,
        "max_miss_points": round(misses[-1], 5) if m else None,
        f"n_missed_by_under_{SPREAD_SCALE_POINTS}_points": near,
        "n_expired_on_a_fillable_bar": n_ttl,
        "live_outcome_of_the_missed": dict(sorted(by_live_outcome.items())),
        "verdict": verdict,
        "note": ("An entry the simulator never took is a trade it never scored. "
                 "These do not enter the R comparison at all, so they are "
                 "INVISIBLE in mean delta R while still changing which signals "
                 "each variant is ranked on — which is why they get their own "
                 "block rather than a line in the confusion matrix."),
    }


def compare_r(sim_trades: Sequence[dict], live_trades: Sequence[dict]) -> dict:
    """Trade-level R agreement. Rows: {signal_id, account_id, r}.

    R, not P&L: the two runs may size differently (equity is a constant here and
    live equity drifts), and R is the scale-free quantity that survives that.
    Trade-level only — CLAUDE.md §2.5."""
    def key(r):
        return (r.get("signal_id"), r.get("account_id"))

    sim_by = {key(r): r for r in sim_trades}
    live_by = {key(r): r for r in live_trades}
    deltas = []
    for k in set(sim_by) & set(live_by):
        a, b = _num(sim_by[k].get("r")), _num(live_by[k].get("r"))
        if a is not None and b is not None:
            deltas.append(a - b)
    return _dist(deltas, "delta R (sim - live), trade level")


def gate(outcome_agreement: Optional[float], r_dist: dict) -> dict:
    """Pass/fail against the stated thresholds, with the reason. Not a score —
    a decision, because §5 asks for an explicit acceptance threshold and a
    threshold you can reinterpret is not one."""
    med = r_dist.get("median_abs")
    mean = r_dist.get("mean")
    failures = []
    if outcome_agreement is None:
        failures.append("no filled legs to compare outcomes on")
    elif outcome_agreement < MIN_OUTCOME_AGREEMENT:
        failures.append(f"outcome agreement {outcome_agreement} < {MIN_OUTCOME_AGREEMENT}")
    if med is None:
        failures.append("no comparable R pairs")
    elif med > MAX_MEDIAN_ABS_DELTA_R:
        failures.append(f"median |delta R| {med} > {MAX_MEDIAN_ABS_DELTA_R}")
    bias = None
    if mean is not None:
        if mean > MAX_ABS_MEAN_DELTA_R:
            bias = "optimistic"
            failures.append(f"systematic optimism: mean delta R {mean} > "
                            f"{MAX_ABS_MEAN_DELTA_R}")
        elif mean < -MAX_ABS_MEAN_DELTA_R:
            bias = "pessimistic"
            failures.append(f"systematic pessimism: mean delta R {mean} < "
                            f"-{MAX_ABS_MEAN_DELTA_R}")
    return {
        "passed": not failures,
        "failures": failures,
        "systematic_bias": bias,
        "thresholds": {"outcome_agreement": MIN_OUTCOME_AGREEMENT,
                       "median_abs_delta_r": MAX_MEDIAN_ABS_DELTA_R,
                       "abs_mean_delta_r": MAX_ABS_MEAN_DELTA_R},
        "note": ("A harness consistently rosier than live is a BLOCKING defect, "
                 "not a calibration offset: every variant it ranks inherits the "
                 "optimism. Do not act on a counterfactual from a failed gate."),
    }


def report(sim_legs, live_legs, sim_trades, live_trades, *,
           include_rows: bool = False) -> dict:
    """The gate report. `include_rows` keeps the per-leg detail — thousands of
    entries on a real run, and useless on a terminal — but the SUMMARY it sits
    inside (agreement rate, matched/unmatched counts, the fill-error
    distribution) is the entire diagnosis and is never dropped.

    Trimmed HERE rather than at the call site, because the call site got it
    wrong: `rep.pop("legs", {}).pop("rows", None)` removed the whole legs block
    and then popped `rows` from the orphan, so `validate` printed a pass/fail
    verdict with no agreement rate and no error distribution behind it. A gate
    whose workings cannot be seen is a coin toss with a log line."""
    legs = compare(sim_legs, live_legs)
    if not include_rows:
        legs.pop("rows", None)
    r = compare_r(sim_trades, live_trades)
    return {"legs": legs, "r": r,
            "gate": gate(legs["outcome"]["agreement_rate"], r),
            "known_execution_reality": [
                "max_open_risk_per_symbol blocked different signals per account",
                "confirm-404 rejects (#150)",
                "fill_price=0 unknown fills (#159)",
                "orphaned armed STOPs (#161)",
                "spread and slippage",
                "the daily-loss breaker",
            ],
            "what_this_gate_cannot_see": WHAT_R_HIDES,
            "note": ("Most P&L variance this week was EXECUTION, not prediction. "
                     "The caps and the breaker are simulated; the reject/404 and "
                     "orphaned-order failure modes are NOT — they are broker "
                     "faults with no candle signature, so the harness is "
                     "structurally optimistic by however often they occurred. "
                     "That is a reason to weight the gate's bias term, not to "
                     "explain a failure away.")}


# R = realized_pl / planned_risk, and ANY pure size multiplier scales both, so
# it divides out. That is exactly why R is the right comparator between arms —
# and exactly why passing this gate is not a statement about sizing.
WHAT_R_HIDES = (
    "This gate is R-BASED, and R is scale-free: any pure size multiplier scales "
    "realized_pl and planned_risk together and cancels. So a PASS says the "
    "simulator picks the same entries, exits and timing as live — it says "
    "NOTHING about whether it sizes them the same. fx_factor, the session risk "
    "multiplier (#81), filtration `scale` actions and the cluster budgeter "
    "(#106) are all invisible here by construction. Sizing fidelity needs the "
    "real account equity and a money-level comparison; until then read "
    "`return_pct` as indicative and rank on R."
)


# --- small numeric helpers ----------------------------------------------------
def _num(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bias_word(sim_better: int, sim_worse: int) -> Optional[str]:
    """Which way the outcome disagreements lean. Balanced counts are noise —
    the simulator resolving a coin-flip bar differently each way. A lean is a
    finding, and it points at where the residual R gap comes from."""
    total = sim_better + sim_worse
    if not total:
        return None
    share = sim_better / total
    if share >= 0.6:
        return "optimistic (the simulation invents outcomes live did not get)"
    if share <= 0.4:
        return "pessimistic (the simulation misses outcomes live did get)"
    return "balanced"


def _adverse_delta(direction, sim_fill: float, live_fill: float) -> Optional[float]:
    """Fill difference re-signed so POSITIVE always means the simulation got the
    WORSE entry, whichever way the trade was going.

    A BUY pays: a higher fill is worse. A SELL receives: a higher fill is
    better. Pooling the raw difference across both lets a systematic entry
    advantage average to zero, which is the one thing this measurement exists to
    catch. None when the direction is unknown — guessing it would invent a sign."""
    d = str(direction or "").upper()
    if d == "BUY":
        return sim_fill - live_fill
    if d == "SELL":
        return live_fill - sim_fill
    return None


def _pos(v) -> Optional[float]:
    """A price that is present AND positive. `fill_price = 0` means unknown, and
    treating it as a number is exactly the bug #159 fixed."""
    f = _num(v)
    return f if (f is not None and f > 0) else None


def _pos_or_none(v) -> Optional[float]:
    """A recorded miss DISTANCE, kept only when it is a real gap. Zero or
    negative means the level was reached and the order failed to fill for some
    other reason — a different finding, and averaging it in would drag the
    median toward "it was basically touching" (#185)."""
    f = _num(v)
    return f if (f is not None and f > 0) else None


def _dist(values: Iterable[float], label: str) -> dict:
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0, "label": label, "mean": None, "median": None,
                "median_abs": None, "p90_abs": None, "max_abs": None}
    absv = sorted(abs(v) for v in vals)
    return {
        "n": len(vals), "label": label,
        "mean": round(sum(vals) / len(vals), 4),
        "median": round(median(vals), 4),
        "median_abs": round(median(absv), 4),
        "p90_abs": round(absv[min(len(absv) - 1, int(0.9 * len(absv)))], 4),
        "max_abs": round(absv[-1], 4),
    }
