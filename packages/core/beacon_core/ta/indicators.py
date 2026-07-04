"""Pure-Python technical indicators operating on lists of floats (oldest→newest).

These are analytical features (not money), so floats are used — never used in the
sizing path. Each returns None when there isn't enough data rather than raising.
"""
from __future__ import annotations

from typing import List, Optional


def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_full(values: List[float], period: int) -> List[Optional[float]]:
    """EMA aligned to `values` (first period-1 entries are None)."""
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n < period or period < 1:
        return out
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    for i in range(period, n):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def ema(values: List[float], period: int) -> Optional[float]:
    s = ema_full(values, period)
    return s[-1] if s and s[-1] is not None else None


def rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def macd(closes: List[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> Optional[dict]:
    if len(closes) < slow:
        return None
    ef = ema_full(closes, fast)
    es = ema_full(closes, slow)
    line = [(a - b) if (a is not None and b is not None) else None
            for a, b in zip(ef, es)]
    compact = [x for x in line if x is not None]
    if not compact:
        return None
    macd_cur = compact[-1]
    if len(compact) < signal:
        return {"macd": macd_cur, "signal": None, "hist": None, "cross": None}
    sig_full = ema_full(compact, signal)
    sig_cur = sig_full[-1]
    hist_cur = (macd_cur - sig_cur) if sig_cur is not None else None
    cross = None
    if len(compact) >= 2 and sig_full[-2] is not None and hist_cur is not None:
        prev_hist = compact[-2] - sig_full[-2]
        if prev_hist <= 0 < hist_cur:
            cross = "up"
        elif prev_hist >= 0 > hist_cur:
            cross = "down"
    return {"macd": macd_cur, "signal": sig_cur, "hist": hist_cur, "cross": cross}


def atr(highs: List[float], lows: List[float], closes: List[float],
        period: int = 14) -> Optional[float]:
    n = len(closes)
    if n < period + 1 or len(highs) < n or len(lows) < n:
        return None
    trs = []
    for i in range(1, n):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    a = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        a = (a * (period - 1) + trs[i]) / period
    return a


def swings(highs: List[float], lows: List[float], k: int = 3) -> tuple[list, list]:
    """Fractal swing highs/lows: a bar that is the extreme of its ±k neighbours."""
    sh, sl = [], []
    for i in range(k, len(highs) - k):
        if highs[i] == max(highs[i - k:i + k + 1]):
            sh.append(highs[i])
        if lows[i] == min(lows[i - k:i + k + 1]):
            sl.append(lows[i])
    return sh, sl


def support_resistance(highs, lows, price, k: int = 3) -> tuple[Optional[float], Optional[float]]:
    """Nearest swing support (below price) and resistance (above price)."""
    sh, sl = swings(highs, lows, k)
    resistance = min([h for h in sh if h > price], default=None)
    support = max([l for l in sl if l < price], default=None)
    return support, resistance


_FIB = {"0.236": 0.236, "0.382": 0.382, "0.5": 0.5, "0.618": 0.618,
        "0.786": 0.786, "1.272": 1.272, "1.618": 1.618}


def fib_levels(highs: List[float], lows: List[float]) -> Optional[dict]:
    """Retracement/extension levels off the window's dominant swing (high↔low),
    directioned by which extreme came last."""
    if not highs or not lows:
        return None
    hi, lo = max(highs), min(lows)
    if hi <= lo:
        return None
    diff = hi - lo
    hi_i = highs.index(hi)
    lo_i = lows.index(lo)
    up = lo_i < hi_i          # low then high => up-swing; fibs measured hi→lo
    levels = {}
    for name, r in _FIB.items():
        levels[name] = (hi - r * diff) if up else (lo + r * diff)
    return {"high": hi, "low": lo, "up_swing": up, "levels": levels}


def nearest_fib(price: float, fib: Optional[dict]) -> Optional[dict]:
    if not fib or not price:
        return None
    best = None
    for name, lvl in fib["levels"].items():
        d = abs(price - lvl) / price
        if best is None or d < best["dist_pct"]:
            best = {"level": name, "price": lvl, "dist_pct": d}
    return best
