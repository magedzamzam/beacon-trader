"""Pure reconciliation: given a signal, the channel's claims, and the bot's legs,
classify the divergence. No DB — the caller loads the rows and passes dicts in.

Categories (precedence order):
  match                        bot reached >= the highest claimed TP
  no_fill                      a trade was placed but no leg ever filled
  shortfall_stopped_before_tp  filled, but closed (SL/BE) before the claimed TP
  shortfall_leg_missing        no leg exists at the claimed TP index
  executed_no_trade            signal marked executed but zero legs placed
  not_executed                 signal never traded (blocked / rejected / no trade)
  claim_sl                     channel claimed only a stop-loss (no TP)
"""
from __future__ import annotations

from typing import List, Optional

_FILLED = ("open", "closed")


def _bot_max_tp(legs) -> int:
    hit = [l.get("tp_index", 0) for l in legs if l.get("outcome") == "tp_hit"]
    return max(hit) if hit else 0


def reconcile_signal(*, signal_status: str, n_signal_tps: int, is_history: bool,
                     claims: List[dict], legs: List[dict]) -> dict:
    """claims: [{max_tp_claimed, sl_claimed, all_tp}]; legs: [{tp_index, status,
    outcome, fill_price}]. Returns the reconciliation summary for one signal."""
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

    if claimed_max_tp <= 0:
        cat = "claim_sl"
        detail = "channel claimed SL" + (" — bot filled" if bot_any_fill else " — bot never filled")
    elif not legs:
        cat = "executed_no_trade" if signal_status == "executed" else "not_executed"
        detail = f"channel claimed TP{claimed_max_tp}, bot placed no legs ({signal_status})"
    elif not bot_any_fill:
        cat = "no_fill"
        detail = f"{n_cancelled}/{len(legs)} legs {'cancelled' if n_cancelled else 'unfilled'}, 0 fills"
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
                  "executed_no_trade", "not_executed")


def is_match(category: str) -> bool:
    return category == "match"
