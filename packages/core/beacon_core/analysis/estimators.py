"""Phase-1 shadow analytics estimators (#53). All SHADOW / measure-before-gate:
compute + persist + log, never gate execution.

Pure-Python (no numpy/scipy) so they're baked into every image and unit-testable
on a bare box. Each `*_estimator(ctx)` is a thin adapter over a pure helper and
returns a JSON-able dict (or None to skip); the k-NN one is async and lazily
imports the DB stack. sidecar.py registers ESTIMATORS into the harness.
"""
from __future__ import annotations

import math
from statistics import pstdev, mean
from typing import List, Optional

from ._util import dig_num as _num, zone_side   # shared analytics helpers (#69)

# --- Regime thresholds (labels only — nothing gates on them) ------------------
ADX_TRENDING = 25.0          # classic ADX trend threshold
HURST_TRENDING = 0.55        # >0.5 persistent/trending
HIGH_VOL_RVOL_PCT = 0.8      # per-bar return stdev (%) above this => high_vol


# ============================ pure helpers ====================================
def realized_vol(closes: List[float]) -> Optional[float]:
    """Per-bar simple-return standard deviation, in percent."""
    cs = [float(c) for c in closes if c is not None]
    if len(cs) < 3:
        return None
    rets = [(cs[i] / cs[i - 1] - 1.0) for i in range(1, len(cs)) if cs[i - 1]]
    if len(rets) < 2:
        return None
    return pstdev(rets) * 100.0


def hurst_rs(series: List[float], min_window: int = 8) -> Optional[float]:
    """Hurst exponent via rescaled-range (R/S) analysis. >0.5 trending, ~0.5
    random walk, <0.5 mean-reverting. Returns None below ~20 points."""
    ts = [float(x) for x in series if x is not None]
    n = len(ts)
    if n < 20:
        return None
    windows = []
    w = min_window
    while w <= n // 2:
        windows.append(w)
        w *= 2
    if len(windows) < 2:
        return None
    xs, ys = [], []
    for w in windows:
        rescaled = []
        for i in range(0, n - w + 1, w):                 # non-overlapping chunks
            chunk = ts[i:i + w]
            m = mean(chunk)
            acc, cum = 0.0, []
            for x in chunk:
                acc += x - m
                cum.append(acc)
            R = max(cum) - min(cum)
            S = pstdev(chunk)
            if S > 0:
                rescaled.append(R / S)
        if rescaled:
            xs.append(math.log(w))
            ys.append(math.log(mean(rescaled)))
    if len(xs) < 2:
        return None
    return _ols_slope(xs, ys)


def _ols_slope(xs: List[float], ys: List[float]) -> Optional[float]:
    k = len(xs)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = k * sxx - sx * sx
    if denom == 0:
        return None
    return (k * sxy - sx * sy) / denom


def kalman_slope(closes: List[float], q: float = 1e-3, r: float = 1.0) -> Optional[dict]:
    """Constant-velocity 1-D Kalman filter over the price series. Returns the
    filtered level and velocity (slope). Denoise TOOL, not a signal (#53).
    State = [level, velocity]; observe level. Pure 2x2 algebra."""
    cs = [float(c) for c in closes if c is not None]
    if len(cs) < 3:
        return None
    level, vel = cs[0], 0.0
    # covariance P (2x2), process noise Q, measurement noise R
    p00, p01, p10, p11 = 1.0, 0.0, 0.0, 1.0
    for z in cs[1:]:
        # --- predict: x = F x, F = [[1,1],[0,1]] ---
        level = level + vel
        # P = F P F^T + Q
        np00 = p00 + p01 + p10 + p11 + q
        np01 = p01 + p11
        np10 = p10 + p11
        np11 = p11 + q
        p00, p01, p10, p11 = np00, np01, np10, np11
        # --- update: H = [1, 0] ---
        s = p00 + r                       # innovation covariance
        if s == 0:
            return None
        k0, k1 = p00 / s, p10 / s         # Kalman gain
        y = z - level                     # innovation
        level += k0 * y
        vel += k1 * y
        # P = (I - K H) P, H = [1, 0] — compute all entries from the old P
        n00 = (1 - k0) * p00
        n01 = (1 - k0) * p01
        n10 = p10 - k1 * p00
        n11 = p11 - k1 * p01
        p00, p01, p10, p11 = n00, n01, n10, n11
    return {"level": round(level, 5), "slope": round(vel, 6), "method": "kalman_cv"}


def vwap_z(price: Optional[float], vwap: Optional[float],
           closes: List[float]) -> Optional[dict]:
    """Signed VWAP deviation, z-scored by the recent price spread. Positive =
    price above VWAP."""
    if price is None or vwap is None:
        return None
    dev = float(price) - float(vwap)
    dev_pct = (dev / float(vwap) * 100.0) if vwap else None
    cs = [float(c) for c in closes if c is not None]
    scale = pstdev(cs) if len(cs) >= 3 else None
    z = (dev / scale) if scale else None
    return {"vwap": round(float(vwap), 5), "deviation": round(dev, 5),
            "deviation_pct": round(dev_pct, 4) if dev_pct is not None else None,
            "z": round(z, 4) if z is not None else None}


def classify_regime(adx: Optional[float], atr_pct: Optional[float],
                    rvol: Optional[float], hurst: Optional[float]) -> str:
    """trending | ranging | high_vol from ADX + ATR% + realized vol + Hurst.
    Volatility dominates (a vol spike is the 07-08-style regime), then trend."""
    if rvol is not None and rvol >= HIGH_VOL_RVOL_PCT:
        return "high_vol"
    if (adx is not None and adx >= ADX_TRENDING) or (hurst is not None and hurst > HURST_TRENDING):
        return "trending"
    return "ranging"


# --- feature access helpers ---------------------------------------------------
def _tf_features(ctx):
    """Indicator block for the analytics timeframe, else the first available."""
    feats = ctx.features or {}
    if ctx.timeframe in feats:
        return feats[ctx.timeframe]
    for v in feats.values():
        if isinstance(v, dict):
            return v
    return {}


def _tf_num(tf, prefix, inner):
    """Read a numeric indicator output by PREFIX match (#111). Persisted feature
    keys embed their params (e.g. "adx_14", "atr_14") via ta.registry.instance_key,
    the same convention as fvg_*/order_block_* (#59). Match the block whose key is
    `prefix` or `prefix_*`, then read `inner`. Also accepts the bare (unsuffixed)
    key so legacy/synthetic feature blocks keep resolving."""
    if not isinstance(tf, dict):
        return None
    for key, block in tf.items():
        if key == prefix or key.startswith(prefix + "_"):
            v = _num(block, inner)
            if v is not None:
                return v
    return None




# ============================ ctx estimators ==================================
def regime(ctx) -> Optional[dict]:
    tf = _tf_features(ctx)
    adx = _tf_num(tf, "adx", "adx")
    atr_pct = _tf_num(tf, "atr", "pct")
    rvol = realized_vol(ctx.closes)
    hurst = hurst_rs(ctx.closes)
    return {"label": classify_regime(adx, atr_pct, rvol, hurst),
            "adx": adx, "atr_pct": atr_pct,
            "realized_vol": round(rvol, 4) if rvol is not None else None,
            "hurst": round(hurst, 4) if hurst is not None else None}


def hurst(ctx) -> Optional[dict]:
    v = hurst_rs(ctx.closes)
    return None if v is None else {"value": round(v, 4), "method": "R/S"}


def kalman(ctx) -> Optional[dict]:
    return kalman_slope(ctx.closes)


def vwap_deviation(ctx) -> Optional[dict]:
    tf = _tf_features(ctx)
    return vwap_z(ctx.price, _num(tf, "vwap", "value"), ctx.closes)


def _feature_vector(analytics: dict):
    """The curated small k-NN feature vector from an analytics dict (curse-of-
    dimensionality guard at low n). None if the core fields are missing."""
    adx = _num(analytics, "regime", "adx")
    atr = _num(analytics, "regime", "atr_pct")
    rvol = _num(analytics, "regime", "realized_vol")
    h = _num(analytics, "hurst", "value")
    slope = _num(analytics, "kalman", "slope")
    z = _num(analytics, "vwap_deviation", "z")
    vec = [adx, atr, rvol, h, slope, z]
    return None if all(x is None for x in vec) else vec


async def knn(ctx, k: int = 5) -> Optional[dict]:
    """k nearest historical signals (same symbol) by feature vector, with their
    realized win-rate/expectancy. Interpretable, no training, degrades gracefully
    at small n. SHADOW ONLY. Lazy DB imports so the module stays pure."""
    if ctx.session is None:
        return None
    from sqlalchemy import select
    from ..db.models import SignalAnalytics, Trade

    # current vector from THIS run's estimators (row not persisted yet)
    cur = _feature_vector({
        "regime": regime(ctx) or {}, "hurst": hurst(ctx) or {},
        "kalman": kalman(ctx) or {}, "vwap_deviation": vwap_deviation(ctx) or {}})
    if cur is None:
        return None

    rows = (await ctx.session.execute(
        select(SignalAnalytics.signal_id, SignalAnalytics.analytics,
               Trade.realized_pl)
        .join(Trade, Trade.signal_id == SignalAnalytics.signal_id)
        .where(SignalAnalytics.symbol == ctx.symbol,
               SignalAnalytics.signal_id != ctx.signal_id))).all()
    cand = []
    for sid, an, pl in rows:
        vec = _feature_vector(an or {})
        if vec is not None and pl is not None:
            cand.append((sid, vec, float(pl)))
    if len(cand) < k:
        return {"k": k, "n_candidates": len(cand), "note": "insufficient history"}

    # per-dimension z-normalization so mixed scales compare fairly
    dims = len(cur)
    cols = [[c[1][d] for c in cand if c[1][d] is not None] for d in range(dims)]
    mu = [mean(col) if col else 0.0 for col in cols]
    sd = [pstdev(col) if len(col) >= 2 else 1.0 for col in cols]
    sd = [s if s else 1.0 for s in sd]

    def _dist(vec):
        tot = 0.0
        for d in range(dims):
            a = cur[d] if cur[d] is not None else mu[d]
            b = vec[d] if vec[d] is not None else mu[d]
            tot += ((a - mu[d]) / sd[d] - (b - mu[d]) / sd[d]) ** 2
        return math.sqrt(tot)

    ranked = sorted(((_dist(v), sid, pl) for sid, v, pl in cand), key=lambda x: x[0])[:k]
    pls = [pl for _, _, pl in ranked]
    wins = sum(1 for pl in pls if pl > 0)
    return {"k": k, "n_candidates": len(cand),
            "win_rate": round(wins / len(pls), 4),
            "expectancy": round(sum(pls) / len(pls), 4),
            "neighbours": [{"signal_id": sid, "distance": round(dist, 4),
                            "realized_pl": round(pl, 2)} for dist, sid, pl in ranked]}


def _htf_alignment(direction, structures) -> str:
    """Signal direction vs the higher-timeframe (1W/1D) structure labels."""
    if not direction:
        return "mixed"
    want = "bull" if direction == "BUY" else "bear"
    labels = [structures[tf].label for tf in ("1w", "1d") if tf in structures]
    if not labels:
        return "mixed"
    agree = sum(1 for l in labels if l == want)
    disagree = sum(1 for l in labels if l in ("bull", "bear") and l != want)
    if agree and not disagree:
        return "aligned"
    if disagree and not agree:
        return "counter"
    return "mixed"


async def structure_magnet(ctx) -> Optional[dict]:
    """Per-signal reference into the ACTIVE persistent structure/magnet map (#61):
    per-TF structure state (label, premium/discount, nearest fib) + nearest magnet
    zone + HTF alignment, tagged with the map version for point-in-time joins. It
    READS the active map (does not recompute). SHADOW ONLY — never gates. Lazy DB
    import so the module stays pure."""
    if ctx.session is None or ctx.price is None:
        return None
    from .structure_map import active_map
    m = await active_map(ctx.session, ctx.symbol)
    if not m:
        return None
    price = float(ctx.price)

    per_tf = {}
    for tf, s in m["structures"].items():
        atr = float(s.atr) if s.atr is not None else None
        fibs = [lv for lv in m["levels_by_tf"].get(tf, [])
                if lv.kind in ("fib_retracement", "fib_extension")]
        nf = None
        if fibs:
            nearest = min(fibs, key=lambda lv: abs(price - float(lv.price)))
            d = abs(price - float(nearest.price))
            nf = {"ratio": float(nearest.ratio) if nearest.ratio is not None else None,
                  "price": round(float(nearest.price), 5),
                  "dist_atr": round(d / atr, 3) if atr else None}
        per_tf[tf] = {"label": s.label,
                      "premium_discount": (float(s.premium_discount)
                                           if s.premium_discount is not None else None),
                      "nearest_fib": nf}

    nearest_zone, nearest_d, within = None, None, []
    for z in m["zones"]:
        lo, hi = float(z.price_low), float(z.price_high)
        ref_atr = float(z.ref_atr) if z.ref_atr else None
        inside = lo <= price <= hi
        d = 0.0 if inside else min(abs(price - lo), abs(price - hi))
        dist_atr = round(d / ref_atr, 3) if ref_atr else None
        side = zone_side(price, lo, hi)
        if nearest_d is None or d < nearest_d:
            nearest_d = d
            nearest_zone = {"zone_id": z.id, "band": [round(lo, 5), round(hi, 5)],
                            "dist_atr": dist_atr, "side": side,
                            "score": float(z.score), "inside": inside}
        if dist_atr is not None and dist_atr <= 2.0:
            within.append({"zone_id": z.id, "dist_atr": dist_atr, "side": side,
                           "score": float(z.score)})

    return {"map_version_id": m["version_id"], "per_tf": per_tf,
            "nearest_zone": nearest_zone, "zones_within_2atr": within,
            "htf_alignment": _htf_alignment(ctx.direction, m["structures"])}


# Registered into the harness by sidecar.py (kept here so estimators stay pure).
ESTIMATORS = {
    "regime": regime,
    "hurst": hurst,
    "kalman": kalman,
    "vwap_deviation": vwap_deviation,
    "knn": knn,
    "structure_magnet": structure_magnet,
}
