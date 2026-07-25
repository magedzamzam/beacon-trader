"""Pure reconciliation: given a signal, the channel's claims, and the bot's legs,
classify the divergence. No DB — the caller loads the rows and passes dicts in.

Categories (precedence order):
  not_executed                 bot deliberately placed nothing (blocked / rejected /
                               skipped / risk-limited) — PROTECTION, not a defect (#136)
  executed_no_trade            signal marked executed, zero legs, and NO block on record
                               — a genuine "said executed, placed nothing" bug (#136)
  no_fill                      legs were placed but no leg ever filled — a bot fill
                               failure, independent of what the channel claimed (#136 pt4)
  claim_sl                     bot filled; channel claimed only a stop-loss (no TP)
  match                        bot reached >= the highest claimed TP
  shortfall_stopped_before_tp  filled, but closed (SL/BE) before the claimed TP
  shortfall_leg_missing        no leg exists at the claimed TP index

Precedence rationale (#136 pt4): "did the bot place legs?" and "did any leg fill?"
are more fundamental than what the channel claimed, so they resolve BEFORE claim_sl.
That surfaces placed-but-0-fill trades as `no_fill` even when the channel only
claimed an SL (the previous ordering short-circuited them into `claim_sl`, which is
why No-Fill went silently empty for weeks despite real zero-fill trades).
"""
from __future__ import annotations

from typing import List, Optional

_FILLED = ("open", "closed")


def _bot_max_tp(legs) -> int:
    hit = [l.get("tp_index", 0) for l in legs if l.get("outcome") == "tp_hit"]
    return max(hit) if hit else 0


def reconcile_signal(*, signal_status: str, n_signal_tps: int, is_history: bool,
                     claims: List[dict], legs: List[dict], blocked: bool = False) -> dict:
    """claims: [{max_tp_claimed, sl_claimed, all_tp}]; legs: [{tp_index, status,
    outcome, fill_price}]. `blocked` is True when the signal carries a block/skip
    event (risk_blocked / ai_blocked / breaker / untrusted / entry_filtered) — a
    deliberate non-trade, so a zero-leg "executed" signal is PROTECTION, not a bug
    (#136 pt2). Returns the reconciliation summary for one signal."""
    claimed_max_tp, claimed_sl = 0, False
    for c in claims:
        m = n_signal_tps if c.get("all_tp") else int(c.get("max_tp_claimed") or 0)
        claimed_max_tp = max(claimed_max_tp, m)
        claimed_sl = claimed_sl or bool(c.get("sl_claimed"))

    filled = [l for l in legs if l.get("status") in _FILLED or l.get("fill_price") is not None]
    bot_any_fill = len(filled) > 0
    bot_max_tp = _bot_max_tp(legs)
    max_leg_tp = max([l.get("tp_index", 0) for l in legs], default=0)
    n_cancelled = sum(1 for l in legs if l.get("status") == "cancelled")

    if not legs:
        # The bot placed nothing. Only a signal marked "executed" with NO block on
        # record is a genuine defect; everything else (blocked / rejected / skipped
        # / risk-limited) is money protection and must NOT drag the match rate down.
        if signal_status == "executed" and not blocked:
            cat = "executed_no_trade"
            detail = f"marked executed but placed no legs, no block on record (claimed TP{claimed_max_tp})"
        else:
            cat = "not_executed"
            detail = f"bot did not trade ({signal_status}{', blocked' if blocked else ''})"
    elif not bot_any_fill:
        cat = "no_fill"
        detail = f"{n_cancelled}/{len(legs)} legs {'cancelled' if n_cancelled else 'unfilled'}, 0 fills"
    elif claimed_max_tp <= 0:
        cat = "claim_sl"
        detail = "channel claimed SL — bot filled"
    elif bot_max_tp >= claimed_max_tp:
        cat = "match"
        detail = f"bot reached TP{bot_max_tp} (claimed TP{claimed_max_tp})"
    elif max_leg_tp < claimed_max_tp:
        cat = "shortfall_leg_missing"
        detail = f"no leg at TP{claimed_max_tp} (max leg TP{max_leg_tp}); bot reached TP{bot_max_tp}"
    else:
        cat = "shortfall_stopped_before_tp"
        detail = f"filled but stopped at TP{bot_max_tp} of claimed TP{claimed_max_tp}"

    return {
        "claimed_max_tp": claimed_max_tp, "claimed_sl": claimed_sl,
        "bot_max_tp": bot_max_tp, "bot_any_fill": bot_any_fill,
        "bot_status": signal_status,
        "category": cat, "detail": detail, "is_history": is_history,
    }


# categories that count as "the bot fell short of the channel" (the actionable gap)
GAP_CATEGORIES = ("no_fill", "shortfall_stopped_before_tp", "shortfall_leg_missing",
                  "executed_no_trade")

# categories where the bot DELIBERATELY did not trade (protection) — excluded from
# the match-rate denominator (#136 pt2). Surfaced as a separate "protected" count.
PROTECTED_CATEGORIES = ("not_executed",)


def is_match(category: str) -> bool:
    return category == "match"


def is_protected(category: str) -> bool:
    return category in PROTECTED_CATEGORIES


# valid operator outcome overrides (#136 pt3) — a follow-up message the parser
# misread ("FULL TP HIT", "REVERSED AND HIT OUR RISK") can be force-tagged.
OVERRIDE_OUTCOMES = ("none", "sl_hit", "breakeven", "all_tp")  # + "tp<N>"


def override_to_claim(override: Optional[str], n_signal_tps: int) -> Optional[dict]:
    """Map an operator outcome override to the claim shape the reconciler consumes
    ({max_tp_claimed, sl_claimed, all_tp}). Returns None when there is no override
    (the parsed claim stands). Accepts sl_hit | breakeven | all_tp | tp<N> (#136)."""
    if not override:
        return None
    o = override.strip().lower()
    if o in ("", "none"):
        return None
    if o == "sl_hit":
        return {"max_tp_claimed": 0, "sl_claimed": True, "all_tp": False}
    if o == "breakeven":
        return {"max_tp_claimed": 0, "sl_claimed": False, "all_tp": False}
    if o == "all_tp":
        return {"max_tp_claimed": int(n_signal_tps or 0), "sl_claimed": False, "all_tp": True}
    if o.startswith("tp"):
        try:
            n = int(o[2:])
        except ValueError:
            return None
        return {"max_tp_claimed": max(0, n), "sl_claimed": False, "all_tp": False}
    return None


def valid_override(override: Optional[str]) -> bool:
    """True if `override` is an acceptable outcome tag (None/'none' clears it)."""
    if override in (None, "", "none"):
        return True
    o = override.strip().lower()
    if o in OVERRIDE_OUTCOMES:
        return True
    if o.startswith("tp"):
        try:
            int(o[2:])
            return True
        except ValueError:
            return False
    return False
