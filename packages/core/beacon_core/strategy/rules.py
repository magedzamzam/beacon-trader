"""SL-move rule engine. Declarative triggers -> actions, evaluated by the
monitor. Multiple rules chain naturally: each fires independently, and the
engine applies the one that tightens the stop the most.

Triggers:
  {"type": "tp_hit", "index": 1}
  {"type": "price_move", "points": 3}
  {"type": "be_lock_at_r", "r": 0.6}   -> fires once the favorable excursion reaches
                                          r x R, where R = |entry - initial_sl| (the
                                          stop distance at OPEN). Self-adapts to each
                                          signal's own risk, so one rule works across
                                          channels whose stop distances vary ~3x (#109).

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
    # The stop distance at position OPEN is the R denominator for be_lock_at_r.
    # It must be the IMMUTABLE original stop — current_sl is unusable because it is
    # mutated as the stop trails. Optional so the trigger fails open when unknown.
    initial_sl: Optional[Decimal] = None   # #109


def _triggered(trigger: dict, ctx: PositionCtx, tps_hit: Set[int]) -> bool:
    t = trigger.get("type")
    if t == "tp_hit":
        return int(trigger.get("index", 0)) in tps_hit
    if t == "price_move":
        pts = Decimal(str(trigger.get("points", 0)))
        if ctx.side == "BUY":
            return (ctx.current_price - ctx.entry) >= pts
        return (ctx.entry - ctx.current_price) >= pts
    if t == "be_lock_at_r":                       # R-relative favorable excursion (#109)
        if ctx.initial_sl is None:
            return False                          # fail-open: no R denominator known
        R = abs(ctx.entry - ctx.initial_sl)
        if R <= 0:
            return False
        r_mult = Decimal(str(trigger.get("r", 0)))
        moved = (ctx.current_price - ctx.entry) if ctx.side == "BUY" \
            else (ctx.entry - ctx.current_price)
        return moved >= r_mult * R
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


def levels_reached(direction: str, price, tp_levels: Dict[int, Decimal]) -> Set[int]:
    """TP indices whose price level the market has reached, over the FULL ladder.

    `tp_levels` maps tp_index -> level for **all** of a trade's legs (any status),
    not just the open ones. Hit-detection has to run off the full ladder so a TP
    whose leg already closed or expired still counts as reached once price trades
    through it. This is load-bearing for staged trades (#148): the confirmation-
    staging partition can assign the BE-trigger TP (e.g. TP2) to a *reclaim* STOP
    leg that EXPIRES without ever closing `tp_hit`; detecting hits off open legs
    only dropped that TP index, so the runner's SL never ratcheted to breakeven
    while the single-shot arms did. Union this with the persisted set of
    `tp_hit`-closed legs so a TP that was reached then retraced still holds."""
    hit: Set[int] = set()
    for idx, lvl in tp_levels.items():
        lvl = Decimal(str(lvl))
        if direction == "BUY" and price >= lvl:
            hit.add(idx)
        elif direction == "SELL" and price <= lvl:
            hit.add(idx)
    return hit


def next_mfe(direction: str, prev, price) -> Decimal:
    """The updated max-favorable excursion: the best price the trade has printed —
    HIGHEST for BUY, LOWEST for SELL. `prev` is the stored MFE (None on the first
    tick, where the live price seeds it). Feeding this (instead of the instantaneous
    price) into `levels_reached` latches a TP the market reached then retraced
    BETWEEN polls, so a still-open staged runner ratchets its SL like the single-shot
    LIMIT arms that fill at broker granularity (#149). Monotonic: never walks back."""
    price = Decimal(str(price))
    if prev is None:
        return price
    prev = Decimal(str(prev))
    return max(prev, price) if direction == "BUY" else min(prev, price)
