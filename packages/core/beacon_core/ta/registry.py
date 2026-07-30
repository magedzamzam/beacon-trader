"""Indicator registry — the single source of truth for what's available.

Nothing about the indicator set is hardcoded downstream: the collector/capture,
the API catalog, and the frontend all read from REGISTRY. To add an indicator,
add one entry here (id, label, category, params, compute) — it immediately shows
up in the portal, is selectable, and gets captured. No other file changes.

`compute(ctx, params)` returns a small JSON-able dict of outputs (or None if
there isn't enough data). Values are floats/bools; money is never sized here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from . import indicators as I


@dataclass
class Ctx:
    closes: List[float]
    highs: List[float]
    lows: List[float]
    volumes: List[Optional[float]]
    price: float
    opens: Optional[List[float]] = None       # candle opens (for Order Blocks, #59)


AVAILABLE_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
# broker (Capital.com) resolution per timeframe label
TF_RESOLUTION = {
    "1m": "MINUTE", "5m": "MINUTE_5", "15m": "MINUTE_15", "30m": "MINUTE_30",
    "1h": "HOUR", "4h": "HOUR_4", "1d": "DAY",
}


def _r(v, nd: int = 4):
    if isinstance(v, bool) or v is None:
        return v
    return round(v, nd) if isinstance(v, (int, float)) else v


def _rd(d, nd: int = 4):
    if d is None:
        return None
    return {k: _r(v, nd) for k, v in d.items()}


def _P(name, default, mn, mx, typ="int"):
    return {"name": name, "type": typ, "default": default, "min": mn, "max": mx}


# ---- per-indicator compute wrappers (kept tiny; math lives in indicators.py) ----
def _ma(fn):
    def c(ctx, p):
        v = fn(ctx.closes, p["period"])
        if v is None:
            return None
        return {"value": _r(v), "above": (ctx.price > v) if ctx.price else None}
    return c


def _scalar(fn, nd=2):
    def c(ctx, p):
        v = fn(ctx, p)
        return {"value": _r(v, nd)} if v is not None else None
    return c


def _sr(ctx, p):
    sup, res = I.support_resistance(ctx.highs, ctx.lows, ctx.price, p["k"])
    if sup is None and res is None:
        return None
    return {"support": _r(sup), "resistance": _r(res),
            "dist_support_pct": _r((ctx.price - sup) / ctx.price * 100) if sup else None,
            "dist_resistance_pct": _r((res - ctx.price) / ctx.price * 100) if res else None}


def _fib(ctx, p):
    fib = I.fib_levels(ctx.highs, ctx.lows)
    nf = I.nearest_fib(ctx.price, fib)
    if not nf:
        return None
    return {"nearest": nf["level"], "price": _r(nf["price"]),
            "dist_pct": _r(nf["dist_pct"] * 100),
            "up_swing": fib.get("up_swing") if fib else None}


def _atr(ctx, p):
    a = I.atr(ctx.highs, ctx.lows, ctx.closes, p["period"])
    if a is None:
        return None
    return {"value": _r(a, 5), "pct": _r(a / ctx.price * 100, 4) if ctx.price else None}


def _vwap(ctx, p):
    v = I.vwap(ctx.highs, ctx.lows, ctx.closes, ctx.volumes)
    if v is None:
        return None
    return {"value": _r(v), "above": (ctx.price > v) if ctx.price else None}


def _fvg(ctx, p):
    r = I.fair_value_gap(ctx.highs, ctx.lows, ctx.closes, ctx.price,
                         p["min_gap_atr"], p["lookback"])
    if r is None:
        return None
    return {"present": r["present"], "direction": r["direction"],
            "top": _r(r["top"], 5), "bottom": _r(r["bottom"], 5), "mid": _r(r["mid"], 5),
            "size_pct": _r(r["size_pct"]), "dist_pct": _r(r["dist_pct"]),
            "filled": r["filled"]}


def _order_block(ctx, p):
    r = I.order_block(ctx.opens, ctx.highs, ctx.lows, ctx.closes, ctx.price,
                      p["disp_atr"], p["lookback"])
    if r is None:
        return None
    return {"present": r["present"], "type": r["type"],
            "top": _r(r["top"], 5), "bottom": _r(r["bottom"], 5),
            "dist_pct": _r(r["dist_pct"]), "mitigated": r["mitigated"]}


# ---- broadened shadow set (#166) --------------------------------------------
def _pivot_out(levels, price):
    """Round a pivot ladder and add where price sits in it (nearest level + the
    distance to it), which is the part a rule or a fold would actually use."""
    if not levels:
        return None
    out = _rd(levels, 5)
    if price:
        name, lvl = min(levels.items(), key=lambda kv: abs(price - kv[1]))
        out["nearest"] = name
        out["dist_pct"] = _r(abs(price - lvl) / price * 100)
        out["above_p"] = price > levels["p"]
    return out


def _prev_bar(ctx):
    """(high, low, close) of the last COMPLETED bar — pivots are defined on the
    previous period, and the newest bar is still forming."""
    if min(len(ctx.highs), len(ctx.lows), len(ctx.closes)) < 2:
        return None
    return ctx.highs[-2], ctx.lows[-2], ctx.closes[-2]


def _pivots(ctx, p):
    prev = _prev_bar(ctx)
    return _pivot_out(I.pivots(*prev), ctx.price) if prev else None


def _pivot_fib(ctx, p):
    prev = _prev_bar(ctx)
    return _pivot_out(I.pivot_fib(*prev), ctx.price) if prev else None


def _psar(ctx, p):
    d = I.parabolic_sar(ctx.highs, ctx.lows, p["af_step"], p["af_max"])
    if d is None:
        return None
    return {"value": _r(d["value"], 5), "trend": d["trend"],
            "above": (ctx.price > d["value"]) if ctx.price else None}


def _chandelier(ctx, p):
    d = I.chandelier_exit(ctx.highs, ctx.lows, ctx.closes, p["period"], p["mult"])
    return _rd(d, 5)


def _apz(ctx, p):
    d = I.apz(ctx.highs, ctx.lows, ctx.closes, p["period"], p["dev_factor"])
    if d is None:
        return None
    out = _rd(d, 5)
    out["above_upper"] = (ctx.price > d["upper"]) if ctx.price else None
    out["below_lower"] = (ctx.price < d["lower"]) if ctx.price else None
    return out


def _atr_pct_scalar(fn, nd=5):
    """Absolute value + its size relative to price — the comparable form for a
    range measure (an ATR of 3.0 means nothing without the price it's 3.0 of)."""
    def c(ctx, p):
        v = fn(ctx, p)
        if v is None:
            return None
        return {"value": _r(v, nd),
                "pct": _r(v / ctx.price * 100, 4) if ctx.price else None}
    return c


def _kama(ctx, p):
    v = I.kama(ctx.closes, p["period"], p["fast"], p["slow"])
    if v is None:
        return None
    return {"value": _r(v), "above": (ctx.price > v) if ctx.price else None}


def _ichimoku(ctx, p):
    d = I.ichimoku(ctx.highs, ctx.lows, ctx.closes, p["tenkan"], p["kijun"], p["senkou"])
    if d is None:
        return None
    out = _rd(d, 5)
    out["tenkan_above_kijun"] = (d["tenkan"] > d["kijun"]) \
        if (d["tenkan"] is not None and d["kijun"] is not None) else None
    return out


REGISTRY = [
    {"id": "sma", "label": "SMA", "category": "trend",
     "params": [_P("period", 50, 2, 500)], "compute": _ma(I.sma)},
    {"id": "ema", "label": "EMA", "category": "trend",
     "params": [_P("period", 50, 2, 500)], "compute": _ma(I.ema)},
    {"id": "wma", "label": "WMA", "category": "trend",
     "params": [_P("period", 50, 2, 500)], "compute": _ma(I.wma)},
    {"id": "macd", "label": "MACD", "category": "momentum",
     "params": [_P("fast", 12, 2, 100), _P("slow", 26, 2, 200), _P("signal", 9, 2, 100)],
     "compute": lambda ctx, p: _rd(I.macd(ctx.closes, p["fast"], p["slow"], p["signal"]), 5)},
    {"id": "adx", "label": "ADX (+DI/-DI)", "category": "trend",
     "params": [_P("period", 14, 2, 100)],
     "compute": lambda ctx, p: _rd(I.adx(ctx.highs, ctx.lows, ctx.closes, p["period"]), 2)},
    {"id": "aroon", "label": "Aroon", "category": "trend",
     "params": [_P("period", 25, 2, 200)],
     "compute": lambda ctx, p: _rd(I.aroon(ctx.highs, ctx.lows, p["period"]), 2)},
    {"id": "rsi", "label": "RSI", "category": "momentum",
     "params": [_P("period", 14, 2, 200)],
     "compute": _scalar(lambda ctx, p: I.rsi(ctx.closes, p["period"]), 2)},
    {"id": "stoch", "label": "Stochastic", "category": "momentum",
     "params": [_P("k", 14, 2, 100), _P("d", 3, 1, 50)],
     "compute": lambda ctx, p: _rd(I.stochastic(ctx.highs, ctx.lows, ctx.closes, p["k"], p["d"]), 2)},
    {"id": "stochrsi", "label": "Stochastic RSI", "category": "momentum",
     "params": [_P("rsi_period", 14, 2, 100), _P("k", 14, 2, 100)],
     "compute": lambda ctx, p: _rd(I.stoch_rsi(ctx.closes, p["rsi_period"], p["k"]), 2)},
    {"id": "cci", "label": "CCI", "category": "momentum",
     "params": [_P("period", 20, 2, 200)],
     "compute": _scalar(lambda ctx, p: I.cci(ctx.highs, ctx.lows, ctx.closes, p["period"]), 2)},
    {"id": "williams_r", "label": "Williams %R", "category": "momentum",
     "params": [_P("period", 14, 2, 200)],
     "compute": _scalar(lambda ctx, p: I.williams_r(ctx.highs, ctx.lows, ctx.closes, p["period"]), 2)},
    {"id": "roc", "label": "Rate of Change %", "category": "momentum",
     "params": [_P("period", 12, 1, 200)],
     "compute": _scalar(lambda ctx, p: I.roc(ctx.closes, p["period"]), 3)},
    {"id": "momentum", "label": "Momentum", "category": "momentum",
     "params": [_P("period", 10, 1, 200)],
     "compute": _scalar(lambda ctx, p: I.momentum(ctx.closes, p["period"]), 4)},
    {"id": "atr", "label": "ATR", "category": "volatility",
     "params": [_P("period", 14, 2, 200)], "compute": _atr},
    {"id": "bbands", "label": "Bollinger Bands", "category": "volatility",
     "params": [_P("period", 20, 2, 200), _P("stddev", 2, 1, 5, "float")],
     "compute": lambda ctx, p: _rd(I.bollinger(ctx.closes, p["period"], p["stddev"]))},
    {"id": "keltner", "label": "Keltner Channel", "category": "volatility",
     "params": [_P("period", 20, 2, 200), _P("mult", 2, 1, 5, "float")],
     "compute": lambda ctx, p: _rd(I.keltner(ctx.highs, ctx.lows, ctx.closes, p["period"], p["mult"]))},
    {"id": "donchian", "label": "Donchian Channel", "category": "volatility",
     "params": [_P("period", 20, 2, 200)],
     "compute": lambda ctx, p: _rd(I.donchian(ctx.highs, ctx.lows, p["period"]))},
    {"id": "hist_vol", "label": "Historical Volatility", "category": "volatility",
     "params": [_P("period", 20, 2, 200)],
     "compute": _scalar(lambda ctx, p: I.hist_vol(ctx.closes, p["period"]), 4)},
    {"id": "obv", "label": "OBV (needs volume)", "category": "volume",
     "params": [], "compute": _scalar(lambda ctx, p: I.obv(ctx.closes, ctx.volumes), 2)},
    {"id": "vwap", "label": "VWAP (needs volume)", "category": "volume",
     "params": [], "compute": _vwap},
    {"id": "support_resistance", "label": "Swing Support/Resistance", "category": "structure",
     "params": [_P("k", 3, 1, 20)], "compute": _sr},
    {"id": "fib", "label": "Fibonacci", "category": "structure",
     "params": [], "compute": _fib},
    {"id": "fvg", "label": "Fair Value Gap", "category": "structure",
     "params": [_P("min_gap_atr", 0.25, 0, 5, "float"), _P("lookback", 50, 3, 300)],
     "compute": _fvg},
    {"id": "order_block", "label": "Order Block", "category": "structure",
     "params": [_P("disp_atr", 1.0, 0, 10, "float"), _P("lookback", 50, 3, 300)],
     "compute": _order_block},

    # ---- broadened shadow set (#166) — SHADOW ONLY until each clears the bar ----
    {"id": "pivots", "label": "Pivot Points (classic)", "category": "structure",
     "params": [], "compute": _pivots},
    {"id": "pivot_fib", "label": "Pivot Points (Fibonacci)", "category": "structure",
     "params": [], "compute": _pivot_fib},
    {"id": "psar", "label": "Parabolic SAR", "category": "trend",
     "params": [_P("af_step", 0.02, 0.001, 0.2, "float"),
                _P("af_max", 0.2, 0.01, 1, "float")],
     "compute": _psar},
    {"id": "vortex", "label": "Vortex (VI+/VI-)", "category": "trend",
     "params": [_P("period", 14, 2, 200)],
     "compute": lambda ctx, p: _rd(I.vortex(ctx.highs, ctx.lows, ctx.closes, p["period"]))},
    {"id": "ichimoku", "label": "Ichimoku", "category": "trend",
     "params": [_P("tenkan", 9, 2, 100), _P("kijun", 26, 2, 200),
                _P("senkou", 52, 2, 400)],
     "compute": _ichimoku},
    {"id": "dema", "label": "DEMA", "category": "trend",
     "params": [_P("period", 50, 2, 500)], "compute": _ma(I.dema)},
    {"id": "tema", "label": "TEMA", "category": "trend",
     "params": [_P("period", 50, 2, 500)], "compute": _ma(I.tema)},
    {"id": "hma", "label": "Hull MA", "category": "trend",
     "params": [_P("period", 50, 2, 500)], "compute": _ma(I.hma)},
    {"id": "zlema", "label": "Zero-Lag EMA", "category": "trend",
     "params": [_P("period", 50, 2, 500)], "compute": _ma(I.zlema)},
    {"id": "kama", "label": "KAMA", "category": "trend",
     "params": [_P("period", 10, 2, 500), _P("fast", 2, 1, 100), _P("slow", 30, 2, 500)],
     "compute": _kama},
    {"id": "tsi", "label": "True Strength Index", "category": "momentum",
     "params": [_P("long", 25, 2, 200), _P("short", 13, 2, 100)],
     "compute": _scalar(lambda ctx, p: I.tsi(ctx.closes, p["long"], p["short"]), 2)},
    {"id": "cmo", "label": "Chande Momentum", "category": "momentum",
     "params": [_P("period", 14, 2, 200)],
     "compute": _scalar(lambda ctx, p: I.cmo(ctx.closes, p["period"]), 2)},
    {"id": "uo", "label": "Ultimate Oscillator", "category": "momentum",
     "params": [_P("short", 7, 2, 100), _P("medium", 14, 2, 200), _P("long", 28, 2, 400)],
     "compute": _scalar(lambda ctx, p: I.ultimate_osc(ctx.highs, ctx.lows, ctx.closes,
                                                      p["short"], p["medium"], p["long"]), 2)},
    {"id": "ao", "label": "Awesome Oscillator", "category": "momentum",
     "params": [_P("fast", 5, 2, 100), _P("slow", 34, 3, 400)],
     "compute": _scalar(lambda ctx, p: I.awesome_osc(ctx.highs, ctx.lows,
                                                     p["fast"], p["slow"]), 4)},
    {"id": "fisher", "label": "Fisher Transform", "category": "momentum",
     "params": [_P("period", 9, 2, 200)],
     "compute": lambda ctx, p: _rd(I.fisher_transform(ctx.highs, ctx.lows, p["period"]))},
    {"id": "elder_ray", "label": "Elder Bull/Bear Power", "category": "momentum",
     "params": [_P("period", 13, 2, 200)],
     "compute": lambda ctx, p: _rd(I.elder_ray(ctx.highs, ctx.lows, ctx.closes,
                                               p["period"]), 5)},
    {"id": "tr", "label": "True Range (raw)", "category": "volatility",
     "params": [],
     "compute": _atr_pct_scalar(lambda ctx, p: I.true_range(ctx.highs, ctx.lows, ctx.closes))},
    {"id": "msd", "label": "Moving Std Dev", "category": "volatility",
     "params": [_P("period", 20, 2, 200)],
     "compute": _atr_pct_scalar(lambda ctx, p: I.stddev(ctx.closes, p["period"]))},
    {"id": "chandelier", "label": "Chandelier Exit", "category": "volatility",
     "params": [_P("period", 22, 2, 200), _P("mult", 3, 1, 10, "float")],
     "compute": _chandelier},
    {"id": "squeeze", "label": "Squeeze (BB in KC)", "category": "volatility",
     "params": [_P("period", 20, 2, 200), _P("bb_mult", 2, 1, 5, "float"),
                _P("kc_mult", 1.5, 1, 5, "float")],
     "compute": lambda ctx, p: _rd(I.squeeze(ctx.highs, ctx.lows, ctx.closes,
                                             p["period"], p["bb_mult"], p["kc_mult"]))},
    {"id": "apz", "label": "Adaptive Price Zone", "category": "volatility",
     "params": [_P("period", 21, 2, 200), _P("dev_factor", 2, 0.5, 10, "float")],
     "compute": _apz},
]

# The output keys each indicator emits — DECLARED, not discovered. The generic
# entry-filtration evaluator (#167) addresses one field of one indicator, and the
# API validator rejects a field that isn't listed here: a rule pointing at a
# misspelled field would otherwise save cleanly and sit there as a permanently
# silent no-op, which is the worst kind of filter. `test_ta_extended` pins these
# against what compute() actually returns, so the two can't drift.
_MA_OUT = ["value", "above"]
_BAND_OUT = ["middle", "upper", "lower"]
_PIVOT_OUT = ["p", "r1", "s1", "r2", "s2", "nearest", "dist_pct", "above_p"]
_OUTPUTS = {
    "sma": _MA_OUT, "ema": _MA_OUT, "wma": _MA_OUT, "dema": _MA_OUT,
    "tema": _MA_OUT, "hma": _MA_OUT, "zlema": _MA_OUT, "kama": _MA_OUT,
    "vwap": _MA_OUT,
    "macd": ["macd", "signal", "hist", "cross"],
    "adx": ["adx", "plus_di", "minus_di", "trending"],
    "aroon": ["up", "down", "osc"],
    "rsi": ["value"], "cci": ["value"], "williams_r": ["value"], "roc": ["value"],
    "momentum": ["value"], "hist_vol": ["value"], "obv": ["value"],
    "tsi": ["value"], "cmo": ["value"], "uo": ["value"], "ao": ["value"],
    "stoch": ["k", "d", "overbought", "oversold"],
    "stochrsi": ["value", "overbought", "oversold"],
    "atr": ["value", "pct"], "tr": ["value", "pct"], "msd": ["value", "pct"],
    "bbands": ["middle", "upper", "lower", "width", "pct_b",
               "above_upper", "below_lower"],
    "keltner": _BAND_OUT, "donchian": ["upper", "lower", "middle"],
    "apz": _BAND_OUT + ["above_upper", "below_lower"],
    "support_resistance": ["support", "resistance",
                           "dist_support_pct", "dist_resistance_pct"],
    "fib": ["nearest", "price", "dist_pct", "up_swing"],
    "fvg": ["present", "direction", "top", "bottom", "mid", "size_pct",
            "dist_pct", "filled"],
    "order_block": ["present", "type", "top", "bottom", "dist_pct", "mitigated"],
    "pivots": _PIVOT_OUT, "pivot_fib": _PIVOT_OUT[:5] + ["r3", "s3"] + _PIVOT_OUT[5:],
    "psar": ["value", "trend", "above"],
    "vortex": ["plus", "minus", "diff", "bullish"],
    "ichimoku": ["tenkan", "kijun", "cloud_a", "cloud_b", "above_cloud",
                 "in_cloud", "chikou_above", "tenkan_above_kijun"],
    "fisher": ["value", "signal"],
    "elder_ray": ["bull", "bear"],
    "chandelier": ["long", "short"],
    "squeeze": ["on", "width_ratio"],
}
for _spec in REGISTRY:
    _spec["outputs"] = list(_OUTPUTS.get(_spec["id"], ["value"]))

_BY_ID = {s["id"]: s for s in REGISTRY}

DEFAULT_CONFIG = {
    "timeframes": ["5m", "15m", "30m", "1h", "4h", "1d"],
    "indicators": [
        {"id": "rsi", "params": {"period": 14}},
        {"id": "ema", "params": {"period": 50}},
        {"id": "ema", "params": {"period": 200}},
        {"id": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
        {"id": "atr", "params": {"period": 14}},
        {"id": "bbands", "params": {"period": 20, "stddev": 2}},
        {"id": "stoch", "params": {"k": 14, "d": 3}},
        {"id": "adx", "params": {"period": 14}},
        {"id": "support_resistance", "params": {"k": 3}},
        {"id": "fib", "params": {}},
        {"id": "fvg", "params": {"min_gap_atr": 0.25, "lookback": 50}},
        {"id": "order_block", "params": {"disp_atr": 1.0, "lookback": 50}},
    ],
}


def _merge_params(spec, params) -> dict:
    out = {}
    for pdef in spec["params"]:
        raw = (params or {}).get(pdef["name"], pdef["default"])
        try:
            val = int(raw) if pdef["type"] == "int" else float(raw)
        except (TypeError, ValueError):
            val = pdef["default"]
        val = max(pdef["min"], min(pdef["max"], val))
        out[pdef["name"]] = val
    return out


def instance_key(spec, params) -> str:
    parts = [spec["id"]]
    for pdef in spec["params"]:
        v = params[pdef["name"]]
        parts.append(str(int(v)) if float(v) == int(v) else str(v))
    return "_".join(parts)


def compute_one(ctx: Ctx, item: dict):
    """(key, outputs) for one config item, or None if unknown/insufficient."""
    spec = _BY_ID.get(item.get("id"))
    if not spec:
        return None
    p = _merge_params(spec, item.get("params"))
    try:
        out = spec["compute"](ctx, p)
    except Exception:
        return None
    if not out:
        return None
    return instance_key(spec, p), out


def resolve_instance(indicator_id: str, params=None) -> Optional[dict]:
    """{id, params, key, outputs} for a registry id with its params merged and
    clamped, or None if the id is unknown.

    The ONE place a config item is turned into the key its outputs land under, so
    the entry-filtration evaluator (#167) and whatever computed the features agree
    on that key by construction instead of by convention."""
    spec = _BY_ID.get(indicator_id)
    if not spec:
        return None
    p = _merge_params(spec, params)
    return {"id": spec["id"], "params": p, "key": instance_key(spec, p),
            "outputs": list(spec.get("outputs") or [])}


def catalog() -> dict:
    return {"timeframes": AVAILABLE_TIMEFRAMES,
            "indicators": [{"id": s["id"], "label": s["label"],
                            "category": s["category"], "params": s["params"],
                            "outputs": s["outputs"]}
                           for s in REGISTRY]}


def sanitize_config(cfg: dict) -> dict:
    """Keep only known timeframes and known indicators (with clamped params)."""
    cfg = cfg or {}
    tfs = [t for t in (cfg.get("timeframes") or []) if t in AVAILABLE_TIMEFRAMES]
    inds = []
    for item in (cfg.get("indicators") or []):
        spec = _BY_ID.get((item or {}).get("id"))
        if not spec:
            continue
        inds.append({"id": spec["id"], "params": _merge_params(spec, item.get("params"))})
    return {"timeframes": tfs or list(DEFAULT_CONFIG["timeframes"]),
            "indicators": inds or list(DEFAULT_CONFIG["indicators"])}
