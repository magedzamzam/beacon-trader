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


# --- extended library ------------------------------------------------------
def wma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    w = list(range(1, period + 1))
    return sum(v * k for v, k in zip(values[-period:], w)) / sum(w)


def stddev(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    seg = values[-period:]
    m = sum(seg) / period
    return (sum((x - m) ** 2 for x in seg) / period) ** 0.5


def bollinger(closes: List[float], period: int = 20, mult: float = 2.0) -> Optional[dict]:
    if len(closes) < period:
        return None
    m = sum(closes[-period:]) / period
    sd = stddev(closes, period)
    if sd is None:
        return None
    upper, lower, price = m + mult * sd, m - mult * sd, closes[-1]
    return {"middle": m, "upper": upper, "lower": lower,
            "width": (upper - lower) / m if m else None,
            "pct_b": (price - lower) / (upper - lower) if upper != lower else None,
            "above_upper": price > upper, "below_lower": price < lower}


def stochastic(highs, lows, closes, k: int = 14, d: int = 3) -> Optional[dict]:
    if len(closes) < k + d:
        return None
    ks = []
    for i in range(k - 1, len(closes)):
        hh, ll = max(highs[i - k + 1:i + 1]), min(lows[i - k + 1:i + 1])
        ks.append(100 * (closes[i] - ll) / (hh - ll) if hh != ll else 50.0)
    kval = ks[-1]
    dval = sum(ks[-d:]) / d if len(ks) >= d else None
    return {"k": kval, "d": dval, "overbought": kval > 80, "oversold": kval < 20}


def stoch_rsi(closes, rsi_period: int = 14, k: int = 14) -> Optional[dict]:
    rs = []
    for i in range(rsi_period + 1, len(closes) + 1):
        r = rsi(closes[:i], rsi_period)
        if r is not None:
            rs.append(r)
    if len(rs) < k:
        return None
    seg = rs[-k:]
    hh, ll = max(seg), min(seg)
    val = 100 * (rs[-1] - ll) / (hh - ll) if hh != ll else 50.0
    return {"value": val, "overbought": val > 80, "oversold": val < 20}


def cci(highs, lows, closes, period: int = 20) -> Optional[float]:
    if len(closes) < period:
        return None
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(closes))]
    seg = tp[-period:]
    m = sum(seg) / period
    md = sum(abs(x - m) for x in seg) / period
    return (tp[-1] - m) / (0.015 * md) if md else None


def williams_r(highs, lows, closes, period: int = 14) -> Optional[float]:
    if len(closes) < period:
        return None
    hh, ll = max(highs[-period:]), min(lows[-period:])
    return -100 * (hh - closes[-1]) / (hh - ll) if hh != ll else None


def roc(closes: List[float], period: int = 12) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    prev = closes[-period - 1]
    return 100 * (closes[-1] - prev) / prev if prev else None


def momentum(closes: List[float], period: int = 10) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    return closes[-1] - closes[-period - 1]


def adx(highs, lows, closes, period: int = 14) -> Optional[dict]:
    n = len(closes)
    if n < 2 * period + 1:
        return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, n):
        up, dn = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))

    def _smooth(x):
        s = sum(x[:period])
        out = [s]
        for i in range(period, len(x)):
            s = s - s / period + x[i]
            out.append(s)
        return out

    tr_s, pdm_s, mdm_s = _smooth(trs), _smooth(plus_dm), _smooth(minus_dm)
    dxs = []
    for i in range(len(tr_s)):
        tr = tr_s[i]
        if tr == 0:
            dxs.append(0.0); continue
        pdi, mdi = 100 * pdm_s[i] / tr, 100 * mdm_s[i] / tr
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0.0)
    if len(dxs) < period:
        return None
    a = sum(dxs[:period]) / period
    for i in range(period, len(dxs)):
        a = (a * (period - 1) + dxs[i]) / period
    tr = tr_s[-1]
    return {"adx": a, "plus_di": 100 * pdm_s[-1] / tr if tr else 0.0,
            "minus_di": 100 * mdm_s[-1] / tr if tr else 0.0, "trending": a > 25}


def aroon(highs, lows, period: int = 25) -> Optional[dict]:
    if len(highs) < period + 1:
        return None
    sh, sl = highs[-(period + 1):], lows[-(period + 1):]
    up = 100 * sh.index(max(sh)) / period
    down = 100 * sl.index(min(sl)) / period
    return {"up": up, "down": down, "osc": up - down}


def donchian(highs, lows, period: int = 20) -> Optional[dict]:
    if len(highs) < period:
        return None
    up, low = max(highs[-period:]), min(lows[-period:])
    return {"upper": up, "lower": low, "middle": (up + low) / 2}


def keltner(highs, lows, closes, period: int = 20, mult: float = 2.0) -> Optional[dict]:
    e, a = ema(closes, period), atr(highs, lows, closes, period)
    if e is None or a is None:
        return None
    return {"middle": e, "upper": e + mult * a, "lower": e - mult * a}


def obv(closes, volumes) -> Optional[float]:
    if not volumes or all(v is None for v in volumes):
        return None
    o = 0.0
    for i in range(1, len(closes)):
        v = volumes[i] or 0
        if closes[i] > closes[i - 1]:
            o += v
        elif closes[i] < closes[i - 1]:
            o -= v
    return o


def vwap(highs, lows, closes, volumes) -> Optional[float]:
    if not volumes or all(v is None for v in volumes):
        return None
    num = den = 0.0
    for i in range(len(closes)):
        v = volumes[i] or 0
        num += (highs[i] + lows[i] + closes[i]) / 3 * v
        den += v
    return num / den if den else None


def pivots(prev_high: float, prev_low: float, prev_close: float) -> dict:
    p = (prev_high + prev_low + prev_close) / 3
    return {"p": p, "r1": 2 * p - prev_low, "s1": 2 * p - prev_high,
            "r2": p + (prev_high - prev_low), "s2": p - (prev_high - prev_low)}


def hist_vol(closes: List[float], period: int = 20) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    rets = [closes[i] / closes[i - 1] - 1
            for i in range(len(closes) - period, len(closes)) if closes[i - 1]]
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    return (sum((r - m) ** 2 for r in rets) / len(rets)) ** 0.5 * (252 ** 0.5)
