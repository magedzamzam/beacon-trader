"""Fanout planner — one ParsedSignal becomes N concrete legs.

    legs = (distinct entry levels) x (take-profit levels)

One leg per TP per entry. A single entry with 3 TPs -> 3 legs; a range entry
(entry_from != entry_to) with 3 TPs -> 6 legs. No templates, no weighting: the
signal's own entries and TPs define the shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

from ..parsing.models import ParsedSignal


@dataclass
class PlannedLeg:
    side: str                       # BUY | SELL
    entry: Decimal                  # limit level, or market price for MARKET
    tp: Decimal
    sl: Decimal
    tp_index: int
    order_type: str                 # MARKET | LIMIT
    lot: Optional[Decimal] = None   # filled by risk.sizing
    risk_cash: Optional[Decimal] = None
    valid: bool = True
    skip_reason: Optional[str] = None


@dataclass
class FanoutPlan:
    symbol: str
    direction: str
    order_type: str
    legs: List[PlannedLeg] = field(default_factory=list)

    @property
    def valid_legs(self) -> List[PlannedLeg]:
        return [l for l in self.legs if l.valid]


def validate_signal(sig: ParsedSignal) -> tuple[bool, Optional[str]]:
    """Geometry gate: SL and every TP on the correct side of entry."""
    e = sig.entry_to
    if sig.direction == "BUY":
        if sig.sl >= e:
            return False, "BUY stop-loss not below entry"
        if any(tp <= e for tp in sig.tps):
            return False, "BUY has a take-profit at/below entry"
    else:
        if sig.sl <= e:
            return False, "SELL stop-loss not above entry"
        if any(tp >= e for tp in sig.tps):
            return False, "SELL has a take-profit at/above entry"
    if not sig.tps:
        return False, "no take-profit levels"
    return True, None


def build_plan(sig: ParsedSignal, *, order_position_type: str,
               current_price: Decimal,
               min_stop_distance: Optional[Decimal] = None) -> FanoutPlan:
    order_type = (order_position_type or "MARKET").upper()
    entries = [sig.entry_from]
    if sig.entry_to != sig.entry_from:
        entries.append(sig.entry_to)

    plan = FanoutPlan(symbol=sig.symbol, direction=sig.direction, order_type=order_type)

    for entry in entries:
        # MARKET fills at the live price; LIMIT rests at the signalled level.
        leg_entry = current_price if order_type == "MARKET" else entry
        for idx, tp in enumerate(sig.tps, start=1):
            leg = PlannedLeg(side=sig.direction, entry=leg_entry, tp=tp,
                             sl=sig.sl, tp_index=idx, order_type=order_type)
            # Broker minimum stop/limit distance: drop only the offending leg.
            if min_stop_distance is not None and abs(tp - leg_entry) < min_stop_distance:
                leg.valid = False
                leg.skip_reason = "tp within broker min distance"
            plan.legs.append(leg)
    return plan
