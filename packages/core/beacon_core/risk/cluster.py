"""Correlation-cluster risk budgeting (#106) — pure math, no DB/network.

Concurrent same-symbol / same-direction signals from different channels are
usually NOT independent bets: the channels copy/echo one market view, so sizing
each at full risk is N× concentration disguised as diversification. When the
market reverses you don't take one SL, you take all of them at once.

This module decides how much risk a NEW cluster member may carry so the cluster's
AGGREGATE planned risk stays within one configured budget — de-sizing the new
signal to fit rather than blocking it (the operator's explicit intent: "don't
stop trading, but don't let risk inflate linearly"). Open members are immutable
(already at the broker), so the only lever is the arriving signal → this is an
ONLINE de-size-to-fit over the remaining budget.

Everything here is a pure function of numbers passed in by the caller (the
executor queries the open cluster members and the config). Kept Decimal-based and
dependency-free so it's baked into every image and unit-testable on a bare box.

SHADOW-FIRST: the executor computes this for every trade and logs/tags it, but
only *applies* the de-size when `cluster_risk.enabled` is true — measure before
gate (CLAUDE.md §2).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

# Config defaults for the `cluster_risk` block inside the `risk_limits` setting.
# enabled:false => SHADOW (compute + log + tag, never change lots). The feature is
# off entirely when the block is absent, so an un-migrated install is unchanged.
CLUSTER_DEFAULTS = {
    "enabled": False,            # False => shadow only; True => actually de-size
    "window_minutes": 30,        # concurrency window for cluster membership
    "allocation": "equal",       # equal | decaying | confidence_weighted
    "decay": 0.5,                # decaying mode: member k gets budget*decay**k
    "budget": None,              # shared cluster budget (account ccy); None => fall
                                 # back to max_open_risk_per_symbol
    "mixed_policy": "off",       # opposite-direction clusters: off | desize_both |
                                 # higher_confidence  (default off = detect + log)
}

ALLOCATION_MODES = ("equal", "decaying", "confidence_weighted")


def _dec(v, default="0") -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(default)


def merge_config(raw: Optional[dict]) -> Optional[dict]:
    """Overlay a `cluster_risk` config block on the defaults. Returns None when the
    block is absent/empty — the caller treats that as 'feature off entirely' so an
    install without the migration is byte-identical to today."""
    if not raw:
        return None
    cfg = dict(CLUSTER_DEFAULTS)
    cfg.update({k: v for k, v in raw.items() if v is not None})
    if cfg.get("allocation") not in ALLOCATION_MODES:
        cfg["allocation"] = "equal"
    return cfg


@dataclass
class ClusterMember:
    """One already-open trade in the cluster (immutable — at the broker)."""
    planned_risk: Decimal
    weight: Decimal = Decimal("1")     # confidence weight (channel edge), optional


def resolve_budget(cluster_cfg: dict, max_open_risk_per_symbol) -> Decimal:
    """The shared cluster budget in account currency: explicit `budget` if set,
    else the existing per-symbol open-risk cap (the natural aggregate ceiling).
    0 / unset everywhere => 0 (caller treats 0 as 'no budget => no de-size')."""
    b = cluster_cfg.get("budget")
    if b is not None and _dec(b) > 0:
        return _dec(b)
    return _dec(max_open_risk_per_symbol)


def allocate(new_risk, existing: List[ClusterMember], *, budget,
             mode: str = "equal", decay="0.5",
             new_weight="1") -> dict:
    """How much risk the NEW member may carry so the cluster aggregate ≤ budget.

    Open members are immutable, so we fit the arrival into the REMAINING budget
    (budget − Σ existing risk). Never sizes up — `scale` ∈ [0, 1].

    modes:
      equal               target = min(new_risk, remaining)            [de-size-to-fit]
      decaying            target = min(new_risk, remaining, budget*decay**k)
                          (k = #existing; confirmation adds info, but not linearly)
      confidence_weighted target = min(new_risk, remaining) * weight
                          (weight ∈ (0,1]; proven channels keep more of the budget.
                           weight defaults to 1 → identical to equal-fit)

    Returns a JSON-able dict: the target risk, the de-size scale to apply to legs,
    the cluster size, and the aggregate before/after — everything the executor
    needs to log (shadow) or enforce.
    """
    new_risk = _dec(new_risk)
    budget = _dec(budget)
    k = len(existing)
    existing_agg = sum((_dec(m.planned_risk) for m in existing), Decimal(0))

    # No budget configured (0) => feature can't bound anything; pass through.
    if budget <= 0:
        return {"cluster_size": k + 1, "budget": str(budget),
                "aggregate_before": str(existing_agg + new_risk),
                "aggregate_after": str(existing_agg + new_risk),
                "target_risk": str(new_risk), "scale": "1", "mode": mode,
                "limited": False}

    remaining = budget - existing_agg
    if remaining < 0:
        remaining = Decimal(0)

    if mode == "decaying":
        cap_k = budget * (_dec(decay) ** k)
        target = min(new_risk, remaining, cap_k)
    elif mode == "confidence_weighted":
        w = _dec(new_weight)
        if w < 0:
            w = Decimal(0)
        if w > 1:
            w = Decimal(1)
        target = min(new_risk, remaining) * w
    else:  # equal (de-size-to-fit)
        target = min(new_risk, remaining)

    if target < 0:
        target = Decimal(0)
    scale = (target / new_risk) if new_risk > 0 else Decimal(0)
    if scale > 1:
        scale = Decimal(1)

    return {"cluster_size": k + 1, "budget": str(budget),
            "aggregate_before": str(existing_agg + new_risk),
            "aggregate_after": str(existing_agg + target),
            "target_risk": str(target), "scale": str(scale), "mode": mode,
            "limited": bool(scale < 1)}


def mixed_exposure(direction: str, same_dir_risks: List, opp_dir_risks: List) -> Optional[dict]:
    """Opposite-direction concurrency on the same symbol: 5 BUY + 3 SELL open at
    once nets to ~2 units of direction but pays 8 spreads and guarantees one side
    loses. Detect + quantify for logging (policy handled by the caller). Returns
    None when there is no opposite-direction exposure (not a mixed cluster)."""
    if not opp_dir_risks:
        return None
    same = sum((_dec(r) for r in same_dir_risks), Decimal(0))
    opp = sum((_dec(r) for r in opp_dir_risks), Decimal(0))
    net = abs(same - opp)
    gross = same + opp
    net_side = direction if same >= opp else ("SELL" if direction == "BUY" else "BUY")
    return {"same_dir_count": len(same_dir_risks), "opp_dir_count": len(opp_dir_risks),
            "same_dir_risk": str(same), "opp_dir_risk": str(opp),
            "net_exposure": str(net), "gross_exposure": str(gross),
            "net_side": net_side}
