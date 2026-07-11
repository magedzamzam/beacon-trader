"""Assemble a timeframe's indicator snapshot from a config-driven indicator list.

The set of indicators is NOT hardcoded — it comes from the caller's config
(ultimately the portal, stored in the `ta` setting) and is dispatched through
the registry. Values are rounded floats/bools, JSON-friendly.
"""
from __future__ import annotations

from typing import List, Optional

from .registry import Ctx, compute_one

MIN_BARS = 30          # not enough history below this -> skip the timeframe


def compute_timeframe(bars: List[dict], price: Optional[float],
                      indicators: List[dict]) -> Optional[dict]:
    """bars: [{o,h,l,c,v}] oldest→newest. price: reference (live mid) or None.
    indicators: list of {id, params} config items. Returns {instance_key: {..}}."""
    closes = [float(b["c"]) for b in bars if b.get("c") is not None]
    highs = [float(b["h"]) for b in bars if b.get("h") is not None]
    lows = [float(b["l"]) for b in bars if b.get("l") is not None]
    if len(closes) < MIN_BARS or len(highs) < MIN_BARS or len(lows) < MIN_BARS:
        return None
    volumes = [float(b["v"]) if b.get("v") is not None else None for b in bars]
    # Opens for Order-Block detection (#59) — only when every bar has one, so the
    # OHLC arrays stay index-aligned; otherwise OB degrades to None (never blocks).
    opens = [float(b["o"]) for b in bars if b.get("o") is not None]
    if len(opens) != len(closes):
        opens = None
    ctx = Ctx(closes=closes, highs=highs, lows=lows, volumes=volumes,
              price=float(price) if price else closes[-1], opens=opens)

    out: dict = {"_n_bars": len(closes), "_price": round(ctx.price, 4)}
    for item in (indicators or []):
        res = compute_one(ctx, item)
        if res is not None:
            out[res[0]] = res[1]
    return out if len(out) > 2 else None
