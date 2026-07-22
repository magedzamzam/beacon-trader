"""Shared pure helpers for the analytics layer (#69).

DRY consolidation of four small patterns that were copy-pasted across analysis/*
and ta/features. All pure and side-effect-free; read-only research code only —
nothing here touches execution/ingest/monitor. Behaviour is identical to the
originals (kept deliberately so the refactor is a zero-diff move).
"""
from __future__ import annotations

from typing import List, Optional

# overlay_config now lives at the package root so the execution layer can share
# it without crossing the research boundary (#75). Re-exported here so analysis
# call sites keep importing it from `_util`.
from ..confutil import overlay_config  # noqa: F401


def bars_col(bars, key: str) -> List[float]:
    """One OHLCV column as floats, skipping any bar missing that key."""
    return [float(b[key]) for b in (bars or []) if b.get(key) is not None]


def dig(d, *path):
    """Walk a path of dict `.get`s; return the value at the end, or None if any
    step isn't a dict / a key is absent."""
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def dig_num(d, *path):
    """Like dig(), but returns the value only if it's numeric, else None. (Matches
    the originals exactly, including that a bool — an int subclass — passes.)"""
    v = dig(d, *path)
    return v if isinstance(v, (int, float)) else None


def adverse_side(direction: str, side: Optional[str]) -> bool:
    """True when a magnet zone sits on the ADVERSE side of the trade: a BUY into
    a zone above (resistance), or a SELL into one below (support)."""
    return (direction == "BUY" and side == "above") or (direction == "SELL" and side == "below")


def zone_side(price: float, low: float, high: float) -> str:
    """Where `price` sits relative to a zone band: inside | above | below."""
    if low <= price <= high:
        return "inside"
    return "above" if price > high else "below"


def nearest_sides(bands, price):
    """Indices of the nearest RESISTANCE and SUPPORT band around `price` (#116).

    `bands` is a list of (low, high) tuples. Resistance = the nearest band lying
    entirely ABOVE price (low > price); support = the nearest entirely BELOW
    (high < price). Distance is measured to the band's near edge. A band that
    straddles price is neither side. Returns (resistance_idx, support_idx); either
    may be None when that side has no band. Score-agnostic on purpose — so a
    score-ranked list can never hide one side."""
    res_i = sup_i = None
    res_d = sup_d = None
    for i, (lo, hi) in enumerate(bands):
        if lo > price:                                  # band above -> resistance
            d = lo - price
            if res_d is None or d < res_d:
                res_d, res_i = d, i
        elif hi < price:                                # band below -> support
            d = price - hi
            if sup_d is None or d < sup_d:
                sup_d, sup_i = d, i
    return res_i, sup_i
