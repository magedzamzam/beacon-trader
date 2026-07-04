"""Turn a timeframe's OHLC bars into a compact indicator snapshot dict.

Values are rounded floats (JSON-friendly). Booleans capture the qualitative
state a human trader reads: above/below each MA, MACD cross direction, proximity
to swing S/R and Fibonacci levels.
"""
from __future__ import annotations

from typing import List, Optional

from . import indicators as ind

MIN_BARS = 30           # not enough history below this -> skip the timeframe


def _r(v, nd: int = 4):
    return round(v, nd) if isinstance(v, (int, float)) else None


def timeframe_features(bars: List[dict], price: Optional[float]) -> Optional[dict]:
    """bars: list of {o,h,l,c} floats, oldest→newest. price: reference price
    (live mid) or None to use the last close."""
    closes = [float(b["c"]) for b in bars if b.get("c") is not None]
    highs = [float(b["h"]) for b in bars if b.get("h") is not None]
    lows = [float(b["l"]) for b in bars if b.get("l") is not None]
    if len(closes) < MIN_BARS or len(highs) < MIN_BARS or len(lows) < MIN_BARS:
        return None
    p = float(price) if price else closes[-1]

    feat: dict = {"n_bars": len(closes), "price": _r(p),
                  "rsi14": _r(ind.rsi(closes, 14), 2)}

    m = ind.macd(closes)
    if m is not None:
        feat["macd"] = {"macd": _r(m["macd"], 5), "signal": _r(m["signal"], 5),
                        "hist": _r(m["hist"], 5), "cross": m["cross"]}

    for period in (20, 50, 200):
        e = ind.ema(closes, period)
        s = ind.sma(closes, period)
        feat[f"ema{period}"] = _r(e)
        feat[f"sma{period}"] = _r(s)
        feat[f"above_ema{period}"] = (p > e) if e is not None else None
        feat[f"above_sma{period}"] = (p > s) if s is not None else None

    a = ind.atr(highs, lows, closes, 14)
    feat["atr14"] = _r(a, 5)
    feat["atr_pct"] = _r(a / p * 100, 4) if (a is not None and p) else None
    # ATR expansion: current ATR vs ATR up to 10 bars ago.
    a_prev = ind.atr(highs[:-10], lows[:-10], closes[:-10], 14) if len(closes) > 30 else None
    feat["atr_expanding"] = (a > a_prev) if (a is not None and a_prev) else None

    sup, res = ind.support_resistance(highs, lows, p)
    feat["support"] = _r(sup)
    feat["resistance"] = _r(res)
    feat["dist_support_pct"] = _r((p - sup) / p * 100) if sup else None
    feat["dist_resistance_pct"] = _r((res - p) / p * 100) if res else None

    fib = ind.fib_levels(highs, lows)
    nf = ind.nearest_fib(p, fib)
    if nf is not None:
        feat["fib_nearest"] = {"level": nf["level"], "price": _r(nf["price"]),
                               "dist_pct": _r(nf["dist_pct"] * 100)}
        feat["fib_up_swing"] = fib.get("up_swing") if fib else None

    return feat
