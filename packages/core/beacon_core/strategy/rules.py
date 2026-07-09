"""SL-move rule engine. Declarative triggers -> actions, evaluated by the
monitor. Multiple rules chain naturally: each fires independently, and the
engine applies the one that tightens the stop the most.

Triggers:
  {"type": "tp_hit", "index": 1}
  {"type": "price_move", "points": 3}

Actions (move_sl_to):
  {"target": "entry"}
  {"target": "number", "value": 4102}
  {"target": "tp", "index": 1}        -> move SL onto TP1's price
  {"target": "previous_tp"}           -> move SL onto the TP before the one that fired

Example (the classic ratchet):
  TP1 hit  -> SL to entry
  TP2 hit  -> SL to previous_tp   (i.e. TP1)
  TP3 hit  -> SL to previous_tp   (i.e. TP2)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Set

# Default tiered ratchet applied to any source that has no sl_rules of its own:
#   TP1 hit -> SL to entry (breakeven), then TP2 -> TP1, TP3 -> TP2, ...
# Only ever tightens toward profit; covers up to 5 TPs. Override globally via the
# `strategy.default_sl_rules` setting, or per-source in the strategy editor.
DEFAULT_SL_RULES: List[dict] = [
    {"trigger": {"type": "tp_hit", "index": 1}, "action": {"type": "move_sl_to", "target": "entry"}},
    *[{"trigger": {"type": "tp_hit", "index": i},
       "action": {"type": "move_sl_to", "target": "previous_tp"}} for i in range(2, 6)],
]


@dataclass
class PositionCtx:
    side: str                 # BUY | SELL
    entry: Decimal
    current_sl: Optional[Decimal]
    current_price: Decimal
    tps: Dict[int, Decimal] = field(default_factory=dict)   # {1: price, 2: price, ...}


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


def _target_sl(rule: dict, ctx: PositionCtx) -> Optional[Decimal]:
    action = rule.get("action", {})
    if action.get("type") != "move_sl_to":
        return None
    target = action.get("target")
    if target == "entry":
        return ctx.entry
    if target == "number":
        v = action.get("value")
        return Decimal(str(v)) if v is not None else None
    if target == "tp":
        i = int(action.get("index", 0))
        return ctx.tps.get(i)
    if target == "previous_tp":
        trig = rule.get("trigger", {})
        if trig.get("type") != "tp_hit":
            return None
        prev = int(trig.get("index", 0)) - 1
        return ctx.tps.get(prev)
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
        cand = _target_sl(rule, ctx)
        if cand is None or not _is_improvement(ctx.side, cand, ctx.current_sl):
            continue
        if best is None or _is_improvement(ctx.side, cand, best):
            best = cand
    return best
