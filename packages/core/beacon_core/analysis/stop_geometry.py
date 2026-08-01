"""Sub-ATR stop distance: the label and its counterfactual (#189).

THE FINDING THIS MEASURES. Soft-diagnosing every losing filled trade on the
control arm for the frozen week put `tight stop vs 1xATR` first by a distance:

    tight_stop_vs_1ATR        n=20   total_R=-18.19   mean_R=-0.909
    clean_stop_channel_wrong  n=7    total_R= -4.64   mean_R=-0.663
    early_breakeven           n=3    total_R= -0.67   mean_R=-0.224
    (winners)                 n=47   total_R=+16.43   mean_R=+0.350

Stops closer than one ATR cost more than the entire winners' book returned. And
it is a MECHANISM, not a correlation: a stop inside gold's ordinary breathing is
hit by the instrument's own noise rather than by the trade being wrong, which
predicts exactly what is observed — these are stop-outs on the ORIGINAL,
un-ratcheted stop. "Death by breakeven", the historically loud complaint, is now
third and nearly free.

SHIPS DISABLED AND MEASURE-ONLY. Nothing here gates, scales or rejects anything;
`sidecar.py`'s measure-before-gate invariant is preserved. Promotion to an
`entry_filters` variable is a future weekend config act and only after the bar is
met: N>=30 in the direction-folded bucket, a 90% CI excluding the fold's own
rate, surviving leave-one-channel-out and leave-one-day-out, replicated across
at least two epochs. There is not even an evaluator for a stop-geometry gate
today — this is step zero.

PURE — stdlib only.
"""
from __future__ import annotations

from typing import Optional

# The ATR the ratio is measured against. Stated because a ratio is meaningless
# without it: 1x ATR on 5m and 1x ATR on 4h are different distances entirely.
ATR_TIMEFRAME = "1h"
DEFAULT_FLOOR = 1.0

# Outcome of the widen-and-resize counterfactual.
WIDENED_STOPPED = "stopped_at_wider_stop"
WIDENED_REACHED_TP1 = "reached_tp1"
WIDENED_UNRESOLVED = "unresolved"


def atr_abs_from_pct(atr_pct, price) -> Optional[float]:
    """Absolute ATR in PRICE units from `signal_analytics.regime.atr_pct`.

    **THE TRAP THIS FUNCTION EXISTS FOR.** `atr_pct` is a PERCENT OF PRICE, not a
    distance — the registry computes it as `atr_value / price * 100`. Comparing
    `|entry - sl|` against it directly compares dollars to percent and produces a
    confidently wrong verdict: on gold near 4000, an ATR of 0.35% is ~14 points,
    but the raw `0.35` would call every stop wider than 35 cents "wide".

    So the conversion is `(atr_pct / 100) * price`, and it lives in one function
    that every caller goes through."""
    if atr_pct is None or price is None:
        return None
    try:
        atr_pct, price = float(atr_pct), float(price)
    except (TypeError, ValueError):
        return None
    if price <= 0 or atr_pct <= 0:
        return None
    return (atr_pct / 100.0) * price


def stop_geometry(*, entry, sl, atr_pct=None, price=None, atr_abs=None,
                  floor: float = DEFAULT_FLOOR) -> Optional[dict]:
    """The per-signal label: how many ATRs the stop sits from the entry.

    Pass EITHER `atr_pct` + `price` (the persisted form) or a precomputed
    `atr_abs`. Returns None when it cannot be measured — never a default, because
    a missing ATR is not a wide stop."""
    abs_atr = atr_abs if atr_abs is not None else atr_abs_from_pct(atr_pct, price)
    if abs_atr is None or abs_atr <= 0 or entry is None or sl is None:
        return None
    try:
        distance = abs(float(entry) - float(sl))
    except (TypeError, ValueError):
        return None
    if distance <= 0:
        return None
    ratio = distance / abs_atr
    return {
        "atr_timeframe": ATR_TIMEFRAME,
        "atr_abs": round(abs_atr, 5),
        "stop_distance": round(distance, 5),
        "stop_atr_ratio": round(ratio, 4),
        "floor": float(floor),
        "stop_below_atr_floor": bool(ratio < float(floor)),
        "note": ("stop_atr_ratio = |entry - sl| / atr_abs, where atr_abs = "
                 "(atr_pct / 100) * price. atr_pct is a PERCENT OF PRICE; "
                 "comparing the stop distance against it unconverted compares "
                 "dollars to percent."),
    }


def widen_and_resize(*, entry, sl, mfe_r, mae_r, tp1, direction=None,
                     atr_pct=None, price=None, atr_abs=None,
                     floor: float = DEFAULT_FLOOR) -> Optional[dict]:
    """Counterfactual: widen the stop to `floor` x ATR and resize to hold RISK
    constant (#189 item 2).

    THIS IS THE INTERESTING VARIANT, and the reason the issue asks for it rather
    than only for `reject`. Holding cash risk constant means the only thing that
    changes is GEOMETRY — so it tests the mechanism directly instead of measuring
    a volume cut. `reject` reduces exposure and would improve a losing week for
    reasons that have nothing to do with stop placement.

    Scored off the RECONSTRUCTED excursion (#182/#187), because that is the only
    exit-independent record of how far price actually travelled:

      * `mae_r` / `mfe_r` are in R of the ORIGINAL stop, so multiplying by the
        original distance recovers price units;
      * a wider stop survives iff the adverse excursion never reached it;
      * risk is held constant, so a stop-out is still exactly -1R;
      * a survivor that reached TP1 books `tp1_distance / new_stop_distance` —
        which is SMALLER than its original R. That is the honest cost of
        widening, and it is why this is a measurement and not an argument.

    Returns None when the inputs do not support the counterfactual."""
    geo = stop_geometry(entry=entry, sl=sl, atr_pct=atr_pct, price=price,
                        atr_abs=atr_abs, floor=floor)
    if geo is None or mae_r is None or mfe_r is None:
        return None
    original = geo["stop_distance"]
    new_stop = float(floor) * geo["atr_abs"]
    if new_stop <= 0:
        return None
    try:
        mae_abs = abs(float(mae_r)) * original
        mfe_abs = abs(float(mfe_r)) * original
        tp1_abs = abs(float(tp1) - float(entry)) if tp1 is not None else None
    except (TypeError, ValueError):
        return None

    if mae_abs >= new_stop:
        outcome, r = WIDENED_STOPPED, -1.0
    elif tp1_abs is not None and mfe_abs >= tp1_abs:
        outcome, r = WIDENED_REACHED_TP1, tp1_abs / new_stop
    else:
        outcome, r = WIDENED_UNRESOLVED, 0.0
    return {
        **geo,
        "new_stop_distance": round(new_stop, 5),
        "widen_factor": round(new_stop / original, 4),
        "counterfactual_outcome": outcome,
        "counterfactual_r": round(r, 4),
        # The other arm, for contrast. Rejecting is a volume cut: it books
        # nothing, which flatters a losing week for reasons unrelated to the
        # stop. Reported so the two cannot be conflated.
        "reject_r": 0.0,
        "note": ("Risk held CONSTANT — only the geometry changes, so this tests "
                 "the mechanism rather than measuring a reduction in exposure. "
                 "A survivor's TP1 is worth LESS in R against the wider stop; "
                 "that cost is included, not netted out."),
    }


def shadow_label(*, entry, sl, atr_pct=None, price=None, atr_abs=None,
                 mfe_r=None, mae_r=None, tp1=None,
                 floor: float = DEFAULT_FLOOR) -> Optional[dict]:
    """The whole per-signal shadow record: label, and the counterfactual when the
    excursion is available. Measure-only."""
    geo = stop_geometry(entry=entry, sl=sl, atr_pct=atr_pct, price=price,
                        atr_abs=atr_abs, floor=floor)
    if geo is None:
        return None
    cf = widen_and_resize(entry=entry, sl=sl, mfe_r=mfe_r, mae_r=mae_r, tp1=tp1,
                          atr_pct=atr_pct, price=price, atr_abs=atr_abs,
                          floor=floor)
    return {**geo, "counterfactual": cf, "shadow": True}


def rollup(labels) -> dict:
    """Aggregate the shadow labels so a weekly can accumulate N (#189 item 3).

    Reports the below-floor bucket separately, because that is the population any
    future gate would act on — pooling it with the rest would hide exactly the
    effect being measured."""
    rows = [l for l in labels if l]
    below = [l for l in rows if l.get("stop_below_atr_floor")]
    cfs = [l["counterfactual"] for l in below if l.get("counterfactual")]
    actual = [l.get("actual_r") for l in below if l.get("actual_r") is not None]
    cf_r = [c["counterfactual_r"] for c in cfs]
    return {
        "n": len(rows),
        "n_below_floor": len(below),
        "share_below_floor": round(len(below) / len(rows), 4) if rows else None,
        "median_stop_atr_ratio": (
            round(sorted(l["stop_atr_ratio"] for l in rows)[len(rows) // 2], 4)
            if rows else None),
        "below_floor": {
            "n_with_counterfactual": len(cfs),
            "actual_total_r": round(sum(actual), 4) if actual else None,
            "widen_total_r": round(sum(cf_r), 4) if cf_r else None,
            "widen_mean_r": round(sum(cf_r) / len(cf_r), 4) if cf_r else None,
            "n_would_still_have_stopped": sum(
                1 for c in cfs if c["counterfactual_outcome"] == WIDENED_STOPPED),
            "n_would_have_reached_tp1": sum(
                1 for c in cfs if c["counterfactual_outcome"] == WIDENED_REACHED_TP1),
            "reject_total_r": 0.0,
        },
        "n_to_confirm": 30,
        "note": ("SHADOW. Nothing gates on this. Judge only at N>=30 in the "
                 "below-floor bucket, with a 90% CI excluding the fold's own "
                 "rate, surviving leave-one-channel-out and leave-one-day-out, "
                 "and replicated across at least two epochs (#189)."),
    }
