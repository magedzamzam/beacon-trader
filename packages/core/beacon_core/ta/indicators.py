"""Pure-Python technical indicators operating on lists of floats (oldest→newest).

These are analytical features (not money), so floats are used — never used in the
sizing path. Each returns None when there isn't enough data rather than raising.
"""
from __future__ import annotations

import math
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


def pivot_fib(prev_high: float, prev_low: float, prev_close: float) -> dict:
    """Fibonacci pivots: same central pivot, levels at 0.382/0.618/1.0 of the
    previous period's range instead of the classic reflections."""
    p = (prev_high + prev_low + prev_close) / 3
    rng = prev_high - prev_low
    return {"p": p,
            "r1": p + 0.382 * rng, "s1": p - 0.382 * rng,
            "r2": p + 0.618 * rng, "s2": p - 0.618 * rng,
            "r3": p + rng, "s3": p - rng}


def hist_vol(closes: List[float], period: int = 20) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    rets = [closes[i] / closes[i - 1] - 1
            for i in range(len(closes) - period, len(closes)) if closes[i - 1]]
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    return (sum((r - m) ** 2 for r in rets) / len(rets)) ** 0.5 * (252 ** 0.5)


# ---- broadened shadow set (#166) --------------------------------------------
# Formulas taken from the public literature (Wilder, Blau, Chande, Ehlers) with
# FinTA used only as a cross-check on parameter conventions. NOTHING is copied
# from it: FinTA is LGPL-3.0 and pandas-based, and `beacon_core` is pip-installed
# into every python image, so it stays pure-Python over List[float] with zero new
# dependencies. These are SHADOW features — capture and mine them; none gates a
# trade until it clears the promotion bar (CLAUDE.md §2.2).
def _double_ema(values: List[float], period: int) -> Optional[float]:
    """EMA of the EMA — the smoothing primitive TSI and APZ share."""
    first = [x for x in ema_full(values, period) if x is not None]
    if len(first) < period:
        return None
    return ema(first, period)


def parabolic_sar(highs, lows, af_step: float = 0.02,
                  af_max: float = 0.2) -> Optional[dict]:
    """Wilder's Parabolic SAR: a trailing stop that accelerates toward the extreme
    point. `trend` is the side it is currently protecting; `value` is where it sits."""
    n = len(highs)
    if n < 3 or len(lows) < n:
        return None
    up = highs[1] >= highs[0]
    sar = lows[0] if up else highs[0]
    ep = highs[1] if up else lows[1]
    af = af_step
    for i in range(2, n):
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, lows[i - 1], lows[i - 2])
            if lows[i] < sar:                       # flip long -> short
                up, sar, ep, af = False, ep, lows[i], af_step
            elif highs[i] > ep:
                ep, af = highs[i], min(af + af_step, af_max)
        else:
            sar = max(sar, highs[i - 1], highs[i - 2])
            if highs[i] > sar:                      # flip short -> long
                up, sar, ep, af = True, ep, highs[i], af_step
            elif lows[i] < ep:
                ep, af = lows[i], min(af + af_step, af_max)
    return {"value": sar, "trend": "up" if up else "down"}


def vortex(highs, lows, closes, period: int = 14) -> Optional[dict]:
    """Vortex Indicator: VI+ / VI- are the up- and down-movement of the range
    normalised by true range. VI+ > VI- is the bullish reading."""
    n = len(closes)
    if n < period + 1 or len(highs) < n or len(lows) < n:
        return None
    vm_p = [abs(highs[i] - lows[i - 1]) for i in range(1, n)]
    vm_m = [abs(lows[i] - highs[i - 1]) for i in range(1, n)]
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
               abs(lows[i] - closes[i - 1])) for i in range(1, n)]
    tr_sum = sum(trs[-period:])
    if not tr_sum:
        return None
    plus, minus = sum(vm_p[-period:]) / tr_sum, sum(vm_m[-period:]) / tr_sum
    return {"plus": plus, "minus": minus, "diff": plus - minus,
            "bullish": plus > minus}


def tsi(closes: List[float], long: int = 25, short: int = 13) -> Optional[float]:
    """True Strength Index: double-smoothed momentum over double-smoothed |momentum|,
    in percent. Bounded ±100."""
    if len(closes) < long + short + 1:
        return None
    mom = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    num = _double_ema(mom, long)
    den = _double_ema([abs(m) for m in mom], long)
    if num is None or not den:
        return None
    return 100.0 * num / den


def cmo(closes: List[float], period: int = 14) -> Optional[float]:
    """Chande Momentum Oscillator: (up - down) / (up + down) over the window, ±100.
    Unsmoothed, so it swings harder than RSI on the same period."""
    if len(closes) < period + 1:
        return None
    ups = dns = 0.0
    for i in range(len(closes) - period, len(closes)):
        d = closes[i] - closes[i - 1]
        ups += max(d, 0.0)
        dns += max(-d, 0.0)
    tot = ups + dns
    return 100.0 * (ups - dns) / tot if tot else 0.0


def ultimate_osc(highs, lows, closes, short: int = 7, medium: int = 14,
                 long: int = 28) -> Optional[float]:
    """Williams' Ultimate Oscillator: buying pressure over true range, blended
    across three windows (weights 4/2/1) so one timeframe can't dominate."""
    n = len(closes)
    if n < long + 1 or len(highs) < n or len(lows) < n:
        return None
    bp, tr = [], []
    for i in range(1, n):
        lo, hi = min(lows[i], closes[i - 1]), max(highs[i], closes[i - 1])
        bp.append(closes[i] - lo)
        tr.append(hi - lo)

    def _avg(p):
        s = sum(tr[-p:])
        return (sum(bp[-p:]) / s) if s else None

    a1, a2, a3 = _avg(short), _avg(medium), _avg(long)
    if a1 is None or a2 is None or a3 is None:
        return None
    return 100.0 * (4 * a1 + 2 * a2 + a3) / 7.0


def awesome_osc(highs, lows, fast: int = 5, slow: int = 34) -> Optional[float]:
    """Awesome Oscillator: SMA(median price, 5) − SMA(median price, 34)."""
    n = len(highs)
    if n < slow or len(lows) < n:
        return None
    mp = [(highs[i] + lows[i]) / 2 for i in range(n)]
    f, s = sma(mp, fast), sma(mp, slow)
    return None if (f is None or s is None) else f - s


def fisher_transform(highs, lows, period: int = 9) -> Optional[dict]:
    """Ehlers' Fisher Transform of the median price: map the window position into
    (-1, 1), then arctanh it so the tails are sharp. `signal` is the prior bar's
    value (the conventional crossover line)."""
    n = len(highs)
    if n < period + 1 or len(lows) < n:
        return None
    mp = [(highs[i] + lows[i]) / 2 for i in range(n)]
    x = fish = prev = 0.0
    for i in range(period - 1, n):
        seg = mp[i - period + 1:i + 1]
        hi, lo = max(seg), min(seg)
        rng = hi - lo
        raw = 0.0 if rng == 0 else 2.0 * (mp[i] - lo) / rng - 1.0
        x = max(-0.999, min(0.999, 0.33 * raw + 0.67 * x))
        prev, fish = fish, 0.5 * math.log((1 + x) / (1 - x)) + 0.5 * fish
    return {"value": fish, "signal": prev}


def true_range(highs, lows, closes) -> Optional[float]:
    """Raw (unsmoothed) true range of the latest bar — ATR's input, useful on its
    own as a one-bar shock measure."""
    n = len(closes)
    if n < 2 or len(highs) < n or len(lows) < n:
        return None
    return max(highs[-1] - lows[-1], abs(highs[-1] - closes[-2]),
               abs(lows[-1] - closes[-2]))


def chandelier_exit(highs, lows, closes, period: int = 22,
                    mult: float = 3.0) -> Optional[dict]:
    """Chandelier Exit: an ATR trail hung off the window's extreme. `long` is the
    stop for a long position, `short` for a short."""
    if len(highs) < period or len(lows) < period:
        return None
    a = atr(highs, lows, closes, period)
    if a is None:
        return None
    return {"long": max(highs[-period:]) - mult * a,
            "short": min(lows[-period:]) + mult * a}


def squeeze(highs, lows, closes, period: int = 20, bb_mult: float = 2.0,
            kc_mult: float = 1.5) -> Optional[dict]:
    """Squeeze (SQZMI): Bollinger Bands wholly inside the Keltner Channel — a
    volatility contraction. `on` is the squeeze state; `width_ratio` is BB width
    over KC width (< 1 while squeezed)."""
    bb = bollinger(closes, period, bb_mult)
    kc = keltner(highs, lows, closes, period, kc_mult)
    if not bb or not kc:
        return None
    kc_w = kc["upper"] - kc["lower"]
    return {"on": bb["upper"] < kc["upper"] and bb["lower"] > kc["lower"],
            "width_ratio": (bb["upper"] - bb["lower"]) / kc_w if kc_w else None}


def apz(highs, lows, closes, period: int = 21,
        dev_factor: float = 2.0) -> Optional[dict]:
    """Adaptive Price Zone: a double-smoothed EMA band whose half-width is the
    double-smoothed bar range — it widens on volatility instead of on deviation."""
    n = len(closes)
    if n < period or len(highs) < n or len(lows) < n:
        return None
    p = max(1, int(math.ceil(math.sqrt(period))))
    mid = _double_ema(closes, p)
    dev = _double_ema([highs[i] - lows[i] for i in range(n)], p)
    if mid is None or dev is None:
        return None
    band = dev_factor * dev
    return {"middle": mid, "upper": mid + band, "lower": mid - band}


def typical_price(highs, lows, closes) -> Optional[float]:
    """(H + L + C) / 3 of the latest bar — CCI's and VWAP's price input."""
    if not highs or not lows or not closes:
        return None
    return (highs[-1] + lows[-1] + closes[-1]) / 3


def elder_ray(highs, lows, closes, period: int = 13) -> Optional[dict]:
    """Elder's Bull/Bear Power: how far the bar's extremes reach beyond the EMA.
    Bull > 0 with Bear < 0 is the ordinary two-sided bar."""
    e = ema(closes, period)
    if e is None or not highs or not lows:
        return None
    return {"bull": highs[-1] - e, "bear": lows[-1] - e}


# --- MA variants: cheap, but near-collinear with EMA/SMA/WMA by construction.
# Captured so #168's collinearity screen can measure that rather than assume it.
def dema(values: List[float], period: int) -> Optional[float]:
    """Double EMA — 2·EMA − EMA(EMA), less lag than EMA at the same period."""
    first = [x for x in ema_full(values, period) if x is not None]
    if len(first) < period:
        return None
    second = ema(first, period)
    return None if second is None else 2 * first[-1] - second


def tema(values: List[float], period: int) -> Optional[float]:
    """Triple EMA — 3·EMA − 3·EMA² + EMA³."""
    e1 = [x for x in ema_full(values, period) if x is not None]
    if len(e1) < period:
        return None
    e2 = [x for x in ema_full(e1, period) if x is not None]
    if len(e2) < period:
        return None
    e3 = ema(e2, period)
    return None if e3 is None else 3 * e1[-1] - 3 * e2[-1] + e3


def hma(values: List[float], period: int) -> Optional[float]:
    """Hull MA — WMA(2·WMA(n/2) − WMA(n), sqrt(n)). Fast and smooth, at the cost
    of overshoot at turns."""
    half, root = max(1, period // 2), max(1, int(period ** 0.5))
    if len(values) < period + root:
        return None
    raw = []
    for i in range(len(values) - root + 1, len(values) + 1):
        w_half, w_full = wma(values[:i], half), wma(values[:i], period)
        if w_half is None or w_full is None:
            return None
        raw.append(2 * w_half - w_full)
    return wma(raw, root)


def zlema(values: List[float], period: int) -> Optional[float]:
    """Zero-Lag EMA — EMA over the de-lagged series 2·v[i] − v[i−lag]."""
    lag = (period - 1) // 2
    if len(values) < period + lag:
        return None
    de = [2 * values[i] - values[i - lag] for i in range(lag, len(values))]
    return ema(de, period)


def kama(values: List[float], period: int = 10, fast: int = 2,
         slow: int = 30) -> Optional[float]:
    """Kaufman's Adaptive MA: smoothing scaled by the efficiency ratio (net move
    over summed moves), so it tracks in trend and flattens in chop."""
    n = len(values)
    if n < period + 1:
        return None
    fsc, ssc = 2.0 / (fast + 1), 2.0 / (slow + 1)
    k = sum(values[:period]) / period
    for i in range(period, n):
        change = abs(values[i] - values[i - period])
        vol = sum(abs(values[j] - values[j - 1]) for j in range(i - period + 1, i + 1))
        er = change / vol if vol else 0.0
        sc = (er * (fsc - ssc) + ssc) ** 2
        k = k + sc * (values[i] - k)
    return k


def ichimoku(highs, lows, closes, tenkan: int = 9, kijun: int = 26,
             senkou: int = 52) -> Optional[dict]:
    """Ichimoku Kinko Hyo. tenkan/kijun are the current conversion/base lines;
    `cloud_a`/`cloud_b` are the span pair that ACTUALLY applies to the current bar
    (i.e. computed `kijun` bars back, matching the forward plot shift) rather than
    the un-shifted values, so `in_cloud`/`above_cloud` mean what they say. Cloud
    fields are None until there is enough history for the shift."""
    n = len(closes)
    if n < senkou or len(highs) < n or len(lows) < n:
        return None

    def _mid(end, p):
        seg_h, seg_l = highs[end - p + 1:end + 1], lows[end - p + 1:end + 1]
        return (max(seg_h) + min(seg_l)) / 2 if seg_h and seg_l else None

    t, k = _mid(n - 1, tenkan), _mid(n - 1, kijun)
    out = {"tenkan": t, "kijun": k, "cloud_a": None, "cloud_b": None,
           "above_cloud": None, "in_cloud": None, "chikou_above": None}
    j = n - 1 - kijun
    if j >= senkou - 1:
        t0, k0 = _mid(j, tenkan), _mid(j, kijun)
        a, b = (t0 + k0) / 2, _mid(j, senkou)
        price = closes[-1]
        out["cloud_a"], out["cloud_b"] = a, b
        out["above_cloud"] = price > max(a, b)
        out["in_cloud"] = min(a, b) <= price <= max(a, b)
        out["chikou_above"] = closes[-1] > closes[j]
    return out


# ---- market structure: Fair Value Gaps & Order Blocks (#59) ------------------
def _zone_touched(highs, lows, lo, hi, start) -> bool:
    """Did any candle from `start` onward trade inside the price band [lo, hi]?"""
    return any(lows[j] <= hi and highs[j] >= lo for j in range(start, len(highs)))


def fair_value_gap(highs, lows, closes, price, min_gap_atr: float = 0.25,
                   lookback: int = 50) -> Optional[dict]:
    """3-candle imbalance. Bullish FVG when low[t] > high[t-2] (a gap the price
    skipped on the way up); bearish when high[t] < low[t-2]. Reports the NEAREST
    still-unfilled gap to the current price. A gap is `filled` once a later candle
    trades back into it. Gaps smaller than min_gap_atr×ATR are ignored as noise.
    Structure feature only — never gates (measure-before-gate, #59)."""
    n = len(highs)
    if n < 3 or price is None or len(lows) < n:
        return None
    a = atr(highs, lows, closes, 14) or 0.0
    min_gap = max(0.0, float(min_gap_atr)) * a
    start = max(2, n - int(lookback))
    gaps = []
    for t in range(start, n):
        if lows[t] > highs[t - 2]:
            direction, bottom, top = "bull", highs[t - 2], lows[t]
        elif highs[t] < lows[t - 2]:
            direction, bottom, top = "bear", highs[t], lows[t - 2]
        else:
            continue
        if (top - bottom) < min_gap:
            continue
        filled = _zone_touched(highs, lows, bottom, top, t + 1)
        gaps.append((direction, top, bottom, filled))
    if not gaps:
        return {"present": False, "direction": None, "top": None, "bottom": None,
                "mid": None, "size_pct": None, "dist_pct": None, "filled": None}

    def _dist(g):
        _, top, bottom, _ = g
        return 0.0 if bottom <= price <= top else min(abs(price - top), abs(price - bottom))

    unfilled = [g for g in gaps if not g[3]]
    direction, top, bottom, filled = min(unfilled or gaps, key=_dist)
    d = 0.0 if bottom <= price <= top else min(abs(price - top), abs(price - bottom))
    return {"present": not filled, "direction": direction,
            "top": top, "bottom": bottom, "mid": (top + bottom) / 2.0,
            "size_pct": (top - bottom) / price * 100.0,
            "dist_pct": d / price * 100.0, "filled": filled}


def order_block(opens, highs, lows, closes, price, disp_atr: float = 1.0,
                lookback: int = 50) -> Optional[dict]:
    """The last opposing candle before an impulsive displacement. A bullish OB is
    the last down candle before a strong up-move; bearish is the last up candle
    before a strong down-move (impulse body >= disp_atr×ATR). Reports the freshest
    still-unmitigated block (price hasn't returned into its [low, high] zone).
    Needs candle opens; returns None if unavailable. Structure feature only (#59)."""
    n = len(closes)
    if n < 3 or price is None or not opens or len(opens) < n:
        return None
    a = atr(highs, lows, closes, 14) or 0.0
    thr = max(0.0, float(disp_atr)) * a
    if thr <= 0:
        return None
    start = max(1, n - int(lookback))
    blocks = []
    for t in range(start, n):
        body = closes[t] - opens[t]
        if body >= thr and closes[t - 1] < opens[t - 1]:            # bullish OB
            blocks.append(("bull", highs[t - 1], lows[t - 1], t - 1))
        elif -body >= thr and closes[t - 1] > opens[t - 1]:         # bearish OB
            blocks.append(("bear", highs[t - 1], lows[t - 1], t - 1))
    if not blocks:
        return {"present": False, "type": None, "top": None, "bottom": None,
                "dist_pct": None, "mitigated": None}
    scored = [(typ, top, bottom, idx,
               _zone_touched(highs, lows, bottom, top, idx + 2))
              for typ, top, bottom, idx in blocks]
    unmit = [b for b in scored if not b[4]]
    typ, top, bottom, idx, mitigated = max(unmit or scored, key=lambda b: b[3])
    d = 0.0 if bottom <= price <= top else min(abs(price - top), abs(price - bottom))
    return {"present": not mitigated, "type": typ, "top": top, "bottom": bottom,
            "dist_pct": d / price * 100.0, "mitigated": mitigated}
