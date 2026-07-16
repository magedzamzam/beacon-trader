"""Trend-alignment entry filter (#48).

Counter-trend entries (direction fighting the higher-timeframe trend) held ~95%
of the book's realized loss; trend-aligned entries were ~breakeven. This gate
skips or de-sizes a signal whose direction opposes the trend EMA (default 4h
EMA200) at signal time.

CONFIG-DRIVEN and DEFAULT-OFF: the mechanism (don't fight the higher-TF trend)
is regime-agnostic and textbook, but it was validated over a single ~5-day
bearish window (n=118). Keep it behind the A/B flag until re-verified when the
4h trend flips bullish — this is a filter on *alignment*, not a standing
directional bias. Fail-open: a missing/unknown trend never blocks a trade.
"""
from __future__ import annotations

from ..confutil import overlay_config   # layer-neutral known-keys overlay (#75)

# Stored under the `entry_filters` setting as `entry_filters.trend_alignment`.
DEFAULT_TREND_FILTER = {
    "enabled": False,          # A/B flag — opt in per deployment
    "timeframe": "4h",         # trend timeframe (any ta TF_RESOLUTION key)
    "ema_period": 200,         # trend EMA period
    "mode": "skip",            # skip | desize
    "desize_factor": 0.25,     # counter-trend size multiplier when mode == desize
}


def trend_filter_cfg(entry_filters) -> dict:
    """The effective trend-alignment config: defaults overlaid with the stored
    `entry_filters.trend_alignment` block (only known keys)."""
    return overlay_config(DEFAULT_TREND_FILTER, (entry_filters or {}).get("trend_alignment"))


def is_aligned(direction: str, above: bool) -> bool:
    """`above` = price is above the trend EMA (up-trend). A BUY aligns with an
    up-trend; a SELL aligns with a down-trend."""
    return bool(above) if direction == "BUY" else (not bool(above))


def alignment_from_features(features, direction: str,
                            timeframe: str = "4h", ema_period: int = 200):
    """Classify a persisted signal-time TA snapshot as trend-aligned / counter
    (#72 metric): read the `above` flag of the EMA at `timeframe` (captured by
    `ta.compute_timeframe`) and compare against `direction`. Returns True
    (aligned), False (counter) or None when the EMA wasn't captured — the same
    fail-open definition the live filter uses, so the report matches placement."""
    tf = (features or {}).get(timeframe)
    if not isinstance(tf, dict):
        return None
    ema = tf.get("ema_%d" % int(ema_period))
    if not isinstance(ema, dict) or ema.get("above") is None:
        return None
    return is_aligned(direction, ema.get("above"))


def _clamp_factor(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return DEFAULT_TREND_FILTER["desize_factor"]
    return max(0.0, min(1.0, f))


def decide(cfg: dict, direction: str, above) -> tuple:
    """Return (action, size_factor, aligned).

    action: 'allow' | 'skip'. size_factor multiplies the risk budget (1.0 for a
    full-size entry). aligned: True/False, or None when the trend is unknown.

    Disabled config or an unknown trend (`above is None`) always allows at full
    size — the filter never blocks on a missing indicator."""
    if not cfg.get("enabled") or above is None:
        return "allow", 1.0, None
    aligned = is_aligned(direction, above)
    if aligned:
        return "allow", 1.0, True
    # Counter-trend.
    if cfg.get("mode") == "desize":
        f = _clamp_factor(cfg.get("desize_factor", DEFAULT_TREND_FILTER["desize_factor"]))
        if f > 0.0:
            return "allow", f, False
    return "skip", 0.0, False
