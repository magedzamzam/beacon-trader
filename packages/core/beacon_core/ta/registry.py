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
]

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


def catalog() -> dict:
    return {"timeframes": AVAILABLE_TIMEFRAMES,
            "indicators": [{"id": s["id"], "label": s["label"],
                            "category": s["category"], "params": s["params"]}
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
