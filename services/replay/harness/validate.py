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
    outcome_n = outcome_ok = 0
    label_mismatch = 0
    rows: List[dict] = []
    for k in matched:
        s, lv = sim_by[k], live_by[k]
        sf, lf = _pos(s.get("fill_price")), _pos(lv.get("fill_price"))
        d_fill = None
        if sf is not None and lf is not None:
            d_fill = sf - lf
            fill_deltas.append(d_fill)
        agree = None
        if lf is not None:                       # only filled legs have an outcome
            agree = _same_outcome(s.get("outcome"), lv.get("outcome"))
            if agree is not None:
                outcome_n += 1
                outcome_ok += 1 if agree else 0
                if agree and s.get("outcome") != lv.get("outcome"):
                    label_mismatch += 1
        rows.append({"signal_id": k[0], "account_id": k[1], "tp_index": k[2],
                     "sim_outcome": s.get("outcome"), "live_outcome": lv.get("outcome"),
                     "outcome_agrees": agree, "delta_fill": d_fill})

    return {
        "n_matched_legs": len(matched),
        "n_only_sim": len(only_sim), "n_only_live": len(only_live),
        "outcome": {
            "n": outcome_n,
            "agreement_rate": round(outcome_ok / outcome_n, 4) if outcome_n else None,
            "n_stop_family_relabels": label_mismatch,
        },
        "fill": _dist(fill_deltas, "delta_fill (sim - live), instrument points"),
        "rows": rows,
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


def report(sim_legs, live_legs, sim_trades, live_trades) -> dict:
    legs = compare(sim_legs, live_legs)
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
            "note": ("Most P&L variance this week was EXECUTION, not prediction. "
                     "The caps and the breaker are simulated; the reject/404 and "
                     "orphaned-order failure modes are NOT — they are broker "
                     "faults with no candle signature, so the harness is "
                     "structurally optimistic by however often they occurred. "
                     "That is a reason to weight the gate's bias term, not to "
                     "explain a failure away.")}


# --- small numeric helpers ----------------------------------------------------
def _num(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pos(v) -> Optional[float]:
    """A price that is present AND positive. `fill_price = 0` means unknown, and
    treating it as a number is exactly the bug #159 fixed."""
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
