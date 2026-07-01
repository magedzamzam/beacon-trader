"""SL-move rule engine. Declarative triggers -> actions, evaluated by the
monitor. Kept to exactly what's needed now (move SL to entry / to a number),
but the shape extends to trailing without touching callers.

Rule example:
  {"trigger": {"type": "tp_hit", "index": 1},
   "action":  {"type": "move_sl_to", "target": "entry"}}
  {"trigger": {"type": "price_move", "points": 3},
   "action":  {"type": "move_sl_to", "target": "number", "value": 4102}}
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional, Set


@dataclass
class PositionCtx:
    side: str                 # BUY | SELL
    entry: Decimal
    current_sl: Optional[Decimal]
    current_price: Decimal


def _triggered(trigger: dict, ctx: PositionCtx, tps_hit: Set[int]) -> bool:
    t = trigger.get("type")
    if t == "tp_hit":
        return int(trigger.get("index", 0)) in tps_hit
    if t == "price_move":
        pts = Decimal(str(trigger.get("points", 0)))
        if ctx.side == "BUY":
            return (ctx.current_price - ctx.entry) >= pts
        return (ctx.entry - ctx.current_price) >= pts
    return False


def _target_sl(action: dict, ctx: PositionCtx) -> Optional[Decimal]:
    if action.get("type") != "move_sl_to":
        return None
    target = action.get("target")
    if target == "entry":
        return ctx.entry
    if target == "number":
        v = action.get("value")
        return Decimal(str(v)) if v is not None else None
    return None


def _is_improvement(side: str, new_sl: Decimal, cur: Optional[Decimal]) -> bool:
    """Only ever tighten toward profit; never loosen the stop."""
    if cur is None:
        return True
    return new_sl > cur if side == "BUY" else new_sl < cur


def evaluate(ctx: PositionCtx, rules: List[dict],
             tps_hit: Optional[Set[int]] = None) -> Optional[Decimal]:
    """Return a new SL if a rule fires and improves the stop, else None."""
    tps_hit = tps_hit or set()
    best: Optional[Decimal] = None
    for rule in rules or []:
        if not _triggered(rule.get("trigger", {}), ctx, tps_hit):
            continue
        cand = _target_sl(rule.get("action", {}), ctx)
        if cand is None or not _is_improvement(ctx.side, cand, ctx.current_sl):
            continue
        if best is None or _is_improvement(ctx.side, cand, best):
            best = cand
    return best
