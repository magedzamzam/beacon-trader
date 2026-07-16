"""Learned-P(win) execution gate (#64) — Phase 3 of receive→analyze→predict→gate.

Turns the Phase-1/2 Bayesian model (learned on the #63 SIGNAL-QUALITY label) into
a configurable skip / de-size / allow decision, mirroring the trend filter (#48)
so the two compose. This module is PURE (decision + config only).

SHADOW-FIRST, by mandate of the issue: `mode` defaults to "log_only" and
`enabled` to False. `acts_live()` is the single gate on whether the decision may
touch execution — until a would-block-vs-actual report shows the skipped set has
worse expectancy, it only records what it WOULD have done. The decision itself is
computed the same way in both modes so the shadow log is faithful.

Guardrails are enforced, not hoped:
  - Significance: below `min_trades` effective evidence -> observe only.
  - Uncertainty: a credible interval wider than `max_ci_width` -> observe only.
  - Conservatism: skip only when even the UPPER credible bound is below
    `skip_threshold`; the decision reads the CI bounds, never the point estimate.
"""
from __future__ import annotations

from ..confutil import overlay_config

DEFAULT_BAYES_GATE = {
    "enabled": False,          # master A/B flag (per deployment)
    "mode": "log_only",        # log_only | active  -- active must be opted into
    "skip_threshold": 0.40,    # skip when ci_high < this (even best case is poor)
    "desize_threshold": 0.50,  # de-size when ci_high < this (and >= skip)
    "desize_factor": 0.5,      # size multiplier in the de-size band
    "min_trades": 30,          # significance floor: n_eff below this -> observe only
    "max_ci_width": 0.60,      # CI wider than this -> too uncertain -> observe only
}


def gate_cfg(stored) -> dict:
    """Effective gate config: defaults overlaid with the stored `bayes_gate`
    block (known keys only)."""
    return overlay_config(DEFAULT_BAYES_GATE, stored)


def acts_live(cfg: dict) -> bool:
    """Whether the gate may actually alter execution. False in log_only mode (the
    default) or when disabled — the decision is still computed and recorded, just
    not applied. Flipping this to True is the deliberate, per-deployment go-live."""
    return bool(cfg.get("enabled")) and cfg.get("mode") == "active"


def _clamp01(v, default=0.5) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


def decide(cfg: dict, score) -> tuple:
    """Return (action, size_factor, reason).

    action: 'allow' | 'skip'; size_factor multiplies the risk budget (<=1, never
    sizes up — never_increase_risk still applies). `score` is a dict
    {p_win, ci_low, ci_high, n} (n = effective evidence) or None.

    This computes the INTENDED decision from the score and thresholds regardless
    of enabled/mode — callers use acts_live() to decide whether to apply it — so
    the shadow log records exactly what a live gate would have done. Refuses to
    act (allow, 1.0, 'observe_*') below the significance / CI-width guardrails."""
    if not score:
        return "allow", 1.0, "observe_no_score"
    n = int(score.get("n", 0) or 0)
    if n < int(cfg.get("min_trades", 30)):
        return "allow", 1.0, "observe_insufficient_n"
    ci_low, ci_high = score.get("ci_low"), score.get("ci_high")
    if ci_low is None or ci_high is None:
        return "allow", 1.0, "observe_no_ci"
    if (ci_high - ci_low) > float(cfg.get("max_ci_width", 1.0)):
        return "allow", 1.0, "observe_wide_ci"
    if ci_high < float(cfg.get("skip_threshold", 0.40)):
        return "skip", 0.0, "p_win_low"           # even the optimistic bound is poor
    if ci_high < float(cfg.get("desize_threshold", 0.50)):
        f = _clamp01(cfg.get("desize_factor", 0.5))
        return ("allow", f, "p_win_mid") if f > 0.0 else ("skip", 0.0, "p_win_mid")
    return "allow", 1.0, "p_win_ok"
