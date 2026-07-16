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
    # --- confirmation (#79): don't call a signal "aligned" on raw price-vs-EMA
    # alone. The lagging 4h EMA200 mis-scored losing SELLs as aligned at the
    # 07-14 regime turn. An aligned read must ALSO be confirmed by these checks.
    # Each check is fail-open: it only fires when its input is available, so
    # missing data never turns into a false suppression.
    "require_slope": True,       # EMA must slope with the trade (BUY:up / SELL:down)
    "min_dist_atr": 0.5,         # price must be >= this many ATR beyond the EMA (skip the chop band)
    "require_htf_concordance": False,  # optional: a second TF must agree on side
    "htf_timeframe": "1h",       # the concordance timeframe (when the check is on)
    "slope_lookback": 10,        # bars back to measure EMA slope (executor-side)
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


def _confirms(cfg: dict, direction: str, slope, dist_atr, htf_above) -> bool:
    """Whether a price-aligned read is CONFIRMED by the enabled #79 checks.

    Each check applies only when its config flag is on AND its input is present —
    a missing input (None) is fail-open (never converts to a suppression). This
    is what stops the lagging-EMA regime-turn blind spot: a SELL that is 'aligned'
    only because the slow EMA hasn't flipped fails the rising-slope / distance
    checks and is no longer green-lit."""
    if cfg.get("require_slope") and slope is not None:
        slope_ok = (slope > 0) if direction == "BUY" else (slope < 0)
        if not slope_ok:
            return False
    md = cfg.get("min_dist_atr")
    if md and dist_atr is not None and dist_atr < float(md):
        return False
    if cfg.get("require_htf_concordance") and htf_above is not None:
        if not is_aligned(direction, htf_above):
            return False
    return True


def decide(cfg: dict, direction: str, above, *, slope=None, dist_atr=None,
           htf_above=None) -> tuple:
    """Return (action, size_factor, aligned).

    action: 'allow' | 'skip'. size_factor multiplies the risk budget (1.0 for a
    full-size entry). aligned: True/False, or None when the trend is unknown.

    An entry is treated as safely ALIGNED only when price-vs-EMA agrees AND the
    #79 confirmation passes (slope / distance / HTF, each fail-open). A price-
    aligned but UNCONFIRMED read (the regime-turn danger zone) is suppressed just
    like a counter-trend one. When no confirmation inputs are supplied (all None),
    behaviour is identical to the original raw price-vs-EMA filter.

    Disabled config or an unknown trend (`above is None`) always allows at full
    size — the filter never blocks on a missing indicator."""
    if not cfg.get("enabled") or above is None:
        return "allow", 1.0, None
    aligned = is_aligned(direction, above) and _confirms(cfg, direction, slope, dist_atr, htf_above)
    if aligned:
        return "allow", 1.0, True
    # Counter-trend OR price-aligned-but-unconfirmed.
    if cfg.get("mode") == "desize":
        f = _clamp_factor(cfg.get("desize_factor", DEFAULT_TREND_FILTER["desize_factor"]))
        if f > 0.0:
            return "allow", f, False
    return "skip", 0.0, False
