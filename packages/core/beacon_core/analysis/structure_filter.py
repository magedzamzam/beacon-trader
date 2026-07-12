"""Phase-3 filter scaffolding for the structure/magnet map (#61).

DISABLED by default and NOT wired into the executor — measure-before-gate, the
same hard rule as trend_filter (#48). This gives the CONFIG shape (`structure.
filter`) + a pure decision function so that, once Phase-2 Bayesian correlation
shows the edge is real (N>=30 significance), Phase 3 can enable filtering with a
config flip and a single executor hook — no schema or interface change.

The decision reads a signal's `structure_magnet` block (from signal_analytics):
skip/de-size a signal that fires straight into an ADVERSE magnet (a high-score
zone just above a BUY / just below a SELL, within adverse_zone_atr) and/or
against the higher-timeframe structure. Fail-open on missing data.
"""
from __future__ import annotations

DEFAULT_STRUCTURE_FILTER = {
    "enabled": False,             # Phase-1/2 shadow: NEVER gates
    "mode": "skip",               # skip | desize
    "desize_factor": 0.25,
    "adverse_zone_atr": 0.5,      # a magnet within this many ATR on the adverse side
    "require_htf_aligned": False, # also treat HTF-counter structure as a filter reason
}


def _clamp01(v, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, f))


def structure_filter_cfg(structure_cfg) -> dict:
    """The effective filter config from the `structure.filter` block."""
    cfg = dict(DEFAULT_STRUCTURE_FILTER)
    f = (structure_cfg or {}).get("filter")
    if isinstance(f, dict):
        for k in DEFAULT_STRUCTURE_FILTER:
            if k in f:
                cfg[k] = f[k]
    return cfg


def decide(cfg: dict, direction: str, structure_magnet) -> tuple:
    """Return (action, size_factor, reason). action: 'allow' | 'skip'; size_factor
    multiplies the risk budget (1.0 = full). Disabled config or missing structure
    context always allows (fail-open). SHADOW: the executor does NOT call this in
    Phase 1 — it exists so Phase 3 can enable it after the edge is measured."""
    if not cfg.get("enabled") or not structure_magnet:
        return "allow", 1.0, None
    reasons = []
    nz = structure_magnet.get("nearest_zone")
    if nz and nz.get("dist_atr") is not None and nz["dist_atr"] <= float(cfg.get("adverse_zone_atr", 0.5)):
        side = nz.get("side")
        # Adverse: BUY into a zone just ABOVE (resistance), SELL into one just BELOW.
        if (direction == "BUY" and side == "above") or (direction == "SELL" and side == "below"):
            reasons.append("adverse_magnet")
    if cfg.get("require_htf_aligned") and structure_magnet.get("htf_alignment") == "counter":
        reasons.append("htf_counter")
    if not reasons:
        return "allow", 1.0, None
    if cfg.get("mode") == "desize":
        f = _clamp01(cfg.get("desize_factor", 0.25), 0.25)
        if f > 0.0:
            return "allow", f, ",".join(reasons)
    return "skip", 0.0, ",".join(reasons)
