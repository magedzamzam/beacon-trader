"""Persistent multi-timeframe market-structure + Fibonacci magnet engine (#61).

PURE Python (stdlib only) — no DB, no broker, unit-testable on fixture bars. The
persistence/versioning lives in structure_map.py; the per-signal reference lives
in estimators.py (structure_magnet). Shadow-only / measure-before-gate: nothing
here gates or alters execution.

Pipeline: ATR-scaled ZigZag -> HH/HL/LH/LL labels -> bull/bear/range classify ->
Fib ladder (retracement + extension) per leg -> collect levels (fib + swings) ->
cluster across timeframes into magnet zones (Σ weight = confluence strength).

Every level/swing/zone is emitted as a typed, weighted dict so a future signal
engine can persist each as its own queryable row and combine them uniformly with
regime/TA/bayes. See feature_contribution() for the common contract.
"""
from __future__ import annotations

from typing import List, Optional

from ..ta.registry import TF_RESOLUTION
from ._util import bars_col, overlay_config

# Structure timeframes add the weekly bar on top of the TA resolutions.
STRUCT_TF_RESOLUTION = {**TF_RESOLUTION, "1w": "WEEK"}

# Config defaults (settings-driven, seeded like `ta`/`analytics`). Adding a Fib
# ratio here makes it its own structure_levels row automatically.
DEFAULT_STRUCTURE = {
    "enabled": True,                 # shadow observability on by default (never gates)
    "symbols": ["XAUUSD"],
    "timeframes": ["1w", "1d", "4h", "1h", "30m", "15m", "5m", "1m"],
    "fib_retracement": [0.382, 0.5, 0.618, 0.705, 0.786],
    "fib_extension": [1.272, 1.414, 1.618, 2.0, 2.618],
    "zigzag_k_by_tf": {"1w": 2.0, "1d": 2.0, "4h": 1.5, "1h": 1.5,
                       "30m": 1.2, "15m": 1.2, "5m": 1.0, "1m": 1.0},
    "cluster_atr": 0.5,              # zone tolerance = cluster_atr * ATR(1h)
    "max_zone_width_atr": 1.0,       # hard cap on a zone's width = max_zone_width_atr * ATR(1h)
                                     # (stops single-linkage chaining a whole range into one blob)
    "tf_weights": {"1w": 8.0, "1d": 5.0, "4h": 3.0, "1h": 2.0,
                   "30m": 1.5, "15m": 1.2, "5m": 1.0, "1m": 0.8},
    "kind_weights": {"fib_retracement": 1.0, "fib_extension": 0.8,
                     "swing_high": 1.2, "swing_low": 1.2,
                     "equal_high": 1.0, "equal_low": 1.0,
                     "order_block": 1.0, "fvg": 0.8},
    "recompute_cadence_days": 7,
    "min_bars_by_tf": {"1w": 40, "1d": 60, "4h": 80, "1h": 100,
                       "30m": 100, "15m": 120, "5m": 150, "1m": 150},
    "max_bars": 300,
    # Phase-3 filter scaffolding (see structure_filter.py) — DISABLED, not wired
    # into the executor. Present so filtering is a config flip away once measured.
    "filter": {"enabled": False, "mode": "skip", "desize_factor": 0.25,
               "adverse_zone_atr": 0.5, "require_htf_aligned": False},
}


def structure_cfg(stored) -> dict:
    """Effective structure config: defaults overlaid with stored known keys."""
    return overlay_config(DEFAULT_STRUCTURE, stored)


# ============================ pure pipeline ===================================
def zigzag(highs: List[float], lows: List[float], atr: float,
           k: float = 1.5) -> List[dict]:
    """ATR-scaled ZigZag pivots, alternating swing highs/lows. A swing reverses
    when price retraces >= k*ATR from the running extreme. Returns
    [{kind: 'H'|'L', price, idx}] oldest->newest (the final entry is the current,
    still-forming extreme)."""
    n = len(highs)
    if n < 3 or not atr or atr <= 0 or len(lows) < n:
        return []
    thr = float(k) * float(atr)
    pivots: List[dict] = []
    trend = 0                        # 0 undetermined, +1 up (tracking highs), -1 down
    ext_price = highs[0]
    ext_idx = 0
    for i in range(1, n):
        if trend == 0:
            if highs[i] - lows[0] >= thr:
                trend = 1
                pivots.append({"kind": "L", "price": lows[0], "idx": 0})
                ext_price, ext_idx = highs[i], i
            elif highs[0] - lows[i] >= thr:
                trend = -1
                pivots.append({"kind": "H", "price": highs[0], "idx": 0})
                ext_price, ext_idx = lows[i], i
        elif trend == 1:
            if highs[i] > ext_price:
                ext_price, ext_idx = highs[i], i
            elif ext_price - lows[i] >= thr:
                pivots.append({"kind": "H", "price": ext_price, "idx": ext_idx})
                trend = -1
                ext_price, ext_idx = lows[i], i
        else:                        # trend == -1
            if lows[i] < ext_price:
                ext_price, ext_idx = lows[i], i
            elif highs[i] - ext_price >= thr:
                pivots.append({"kind": "L", "price": ext_price, "idx": ext_idx})
                trend = 1
                ext_price, ext_idx = highs[i], i
    if trend == 1:
        pivots.append({"kind": "H", "price": ext_price, "idx": ext_idx})
    elif trend == -1:
        pivots.append({"kind": "L", "price": ext_price, "idx": ext_idx})
    return pivots


def label_swings(pivots: List[dict]) -> List[dict]:
    """Label each pivot HH/LH (a high vs the prior high) or HL/LL (a low vs the
    prior low). The first high/low seeds the baseline (HH / HL)."""
    out = []
    prev_high = prev_low = None
    for p in pivots:
        if p["kind"] == "H":
            kind = "HH" if (prev_high is None or p["price"] > prev_high) else "LH"
            prev_high = p["price"]
        else:
            kind = "HL" if (prev_low is None or p["price"] > prev_low) else "LL"
            prev_low = p["price"]
        out.append({"kind": kind, "price": p["price"], "idx": p["idx"]})
    return out


def classify_structure(labeled: List[dict]) -> str:
    """bull (latest high HH + latest low HL) / bear (LH + LL) / range."""
    last_high = next((s["kind"] for s in reversed(labeled) if s["kind"] in ("HH", "LH")), None)
    last_low = next((s["kind"] for s in reversed(labeled) if s["kind"] in ("HL", "LL")), None)
    if last_high == "HH" and last_low == "HL":
        return "bull"
    if last_high == "LH" and last_low == "LL":
        return "bear"
    return "range"


def active_range(pivots: List[dict]):
    """(low, high) of the current dealing range = most recent swing low & high."""
    last_high = next((p["price"] for p in reversed(pivots) if p["kind"] == "H"), None)
    last_low = next((p["price"] for p in reversed(pivots) if p["kind"] == "L"), None)
    return last_low, last_high


def premium_discount(price: float, low, high) -> Optional[float]:
    """Price position in the active range: 0 = discount (low) -> 1 = premium (high)."""
    if price is None or low is None or high is None or high <= low:
        return None
    return max(0.0, min(1.0, (price - low) / (high - low)))


def fib_ladder(a_price: float, b_price: float, direction: str,
               retr_ratios, ext_ratios) -> List[dict]:
    """Fib levels for the impulse leg A->B. Retracements pull back from B toward
    A; extensions project beyond B in the leg direction. `direction` is 'up'
    (A=low, B=high) or 'down' (A=high, B=low)."""
    rng = b_price - a_price
    levels = []
    for r in retr_ratios:
        levels.append({"kind": "fib_retracement", "ratio": float(r),
                       "price": b_price - float(r) * rng, "direction": direction})
    for e in ext_ratios:
        levels.append({"kind": "fib_extension", "ratio": float(e),
                       "price": b_price + float(e) * rng, "direction": direction})
    return levels


def analyze_timeframe(bars: List[dict], *, atr: float, k: float,
                      retr_ratios, ext_ratios) -> Optional[dict]:
    """Full single-TF analysis. Returns structure summary + its levels (fib
    ladder anchored on the most recent completed leg + swing levels), or None on
    insufficient data. Prices only — money is never sized here."""
    highs = bars_col(bars, "h")
    lows = bars_col(bars, "l")
    closes = bars_col(bars, "c")
    if len(highs) < 5 or len(lows) != len(highs) or not closes:
        return None
    pivots = zigzag(highs, lows, atr, k)
    if len(pivots) < 3:
        return None
    labeled = label_swings(pivots)
    label = classify_structure(labeled)
    low, high = active_range(pivots)
    price = closes[-1]

    # Most recent completed leg = last two confirmed pivots (exclude the still-
    # forming final extreme). A high->low leg is a down-impulse and vice-versa.
    a, b = pivots[-3], pivots[-2]
    direction = "down" if b["price"] < a["price"] else "up"
    levels = fib_ladder(a["price"], b["price"], direction, retr_ratios, ext_ratios)
    anchor_a = {"price": a["price"], "idx": a["idx"]}
    anchor_b = {"price": b["price"], "idx": b["idx"]}
    for lv in levels:
        lv["anchor_a"], lv["anchor_b"] = anchor_a, anchor_b

    # Swing levels (each pivot is its own magnet candidate).
    for p in labeled:
        levels.append({
            "kind": "swing_high" if p["kind"] in ("HH", "LH") else "swing_low",
            "ratio": None, "price": p["price"], "direction": direction,
            "anchor_a": {"price": p["price"], "idx": p["idx"]}, "anchor_b": None,
            "swing_label": p["kind"],
        })

    return {
        "label": label,
        "premium_discount": premium_discount(price, low, high),
        "bias_price": price,
        "swings": [{"kind": s["kind"], "price": round(s["price"], 5), "idx": s["idx"]}
                   for s in labeled][-8:],
        "range_low": low, "range_high": high, "atr": atr,
        "levels": levels,
    }


def cluster_levels(levels: List[dict], tolerance: float,
                   max_width: Optional[float] = None) -> List[dict]:
    """Width-bounded cluster of levels -> magnet zones, scored by Σ(level weight).

    A level joins the current cluster only if it is within `tolerance` of the
    previous member (the chaining gap) AND the cluster's total width would stay
    within `max_width`. The width cap stops single-linkage from *chaining* a dense
    stack of levels into one range-wide "mega-zone" (#113): once a cluster spans
    `max_width`, the next level opens a new zone even if it's within `tolerance`.
    `max_width=None` (or <= 0) disables the cap (legacy single-linkage).

    Each level dict must carry price, weight, timeframe, kind, ratio. Returns zones
    ranked 1 = strongest. Members carry their `weight` so Σ(member weights) == score
    is reconstructable from the stored row."""
    if not levels or tolerance is None or tolerance <= 0:
        return []
    capped = max_width is not None and max_width > 0
    ordered = sorted(levels, key=lambda x: x["price"])
    groups, cur = [], [ordered[0]]
    for lv in ordered[1:]:
        gap_ok = lv["price"] - cur[-1]["price"] <= tolerance
        # cur[0] is the cluster's lowest price (ordered ascending); the width if lv
        # joined is lv - cur[0]. Cap it so a chain can't grow past max_width.
        width_ok = (not capped) or (lv["price"] - cur[0]["price"] <= max_width)
        if gap_ok and width_ok:
            cur.append(lv)
        else:
            groups.append(cur)
            cur = [lv]
    groups.append(cur)

    zones = []
    for g in groups:
        prices = [m["price"] for m in g]
        score = sum(float(m.get("weight", 0.0)) for m in g)
        tfs = {m.get("timeframe") for m in g if m.get("timeframe")}
        zones.append({
            "price_low": min(prices), "price_high": max(prices),
            "mid": sum(prices) / len(prices), "score": round(score, 4),
            "n_timeframes": len(tfs),
            "members": [{"level_id": m.get("level_id"), "timeframe": m.get("timeframe"),
                         "kind": m.get("kind"), "ratio": m.get("ratio"),
                         "price": round(m["price"], 5),
                         "weight": round(float(m.get("weight", 0.0)), 6)} for m in g],
        })
    zones.sort(key=lambda z: (-z["score"], -z["n_timeframes"]))
    for i, z in enumerate(zones):
        z["rank"] = i + 1
    return zones


# ---- composability: the common feature-contribution contract (#61/#70) --------
# Canonical definition now lives in contract.py (the locked estimator envelope);
# re-exported here so existing structure.feature_contribution callers keep working.
from .contract import feature_contribution  # noqa: E402,F401
