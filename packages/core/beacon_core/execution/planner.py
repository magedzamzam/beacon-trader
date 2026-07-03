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


def _entry_crossed(direction: str, entry: Decimal,
                   high: Optional[Decimal], low: Optional[Decimal]) -> bool:
    """Has the market already reached this entry level? A BUY limit fills when
    price falls to entry (so a candle low at/below it = crossed); a SELL limit
    fills when price rises to entry (candle high at/above it = crossed)."""
    if direction == "BUY":
        return low is not None and low <= entry
    return high is not None and high >= entry


def _tp_beyond(direction: str, tp: Decimal, entry: Decimal) -> bool:
    """TP must sit in the profit direction from the (actual) entry."""
    return tp > entry if direction == "BUY" else tp < entry


def _sl_protective(direction: str, sl: Decimal, entry: Decimal) -> bool:
    """SL must sit on the protective side of the (actual) entry."""
    return sl < entry if direction == "BUY" else sl > entry


def build_plan(sig: ParsedSignal, *, current_price: Decimal,
               candle_high: Optional[Decimal] = None,
               candle_low: Optional[Decimal] = None,
               min_stop_distance: Optional[Decimal] = None) -> FanoutPlan:
    """Sources are LIMIT-only, but per leg: if the current candle has already
    crossed an entry level, that leg is opened MARKET now (the price already
    touched the level and may not rebound) at the live price; otherwise it rests
    as a LIMIT at the signalled level. Decided per entry level, not per signal.
    """
    entries = [sig.entry_from]
    if sig.entry_to != sig.entry_from:
        entries.append(sig.entry_to)

    # Fold the live price into the candle range so an in-progress touch counts.
    highs = [x for x in (candle_high, current_price) if x is not None]
    lows = [x for x in (candle_low, current_price) if x is not None]
    hi = max(highs) if highs else None
    lo = min(lows) if lows else None

    # Any already-crossed entry collapses into ONE market fill at the live price
    # (two market legs at the same price would just double the size); each entry
    # still waiting rests as its own LIMIT.
    order_plan = []  # (order_type, leg_entry)
    if any(_entry_crossed(sig.direction, e, hi, lo) for e in entries):
        order_plan.append(("MARKET", current_price))
    for e in entries:
        if not _entry_crossed(sig.direction, e, hi, lo):
            order_plan.append(("LIMIT", e))

    plan = FanoutPlan(symbol=sig.symbol, direction=sig.direction, order_type="LIMIT")

    for order_type, leg_entry in order_plan:
        for idx, tp in enumerate(sig.tps, start=1):
            leg = PlannedLeg(side=sig.direction, entry=leg_entry, tp=tp,
                             sl=sig.sl, tp_index=idx, order_type=order_type)
            # Drop a leg the actual entry has made untradeable, or too tight.
            if not _sl_protective(sig.direction, sig.sl, leg_entry):
                leg.valid = False
                leg.skip_reason = "sl on wrong side of entry"
            elif not _tp_beyond(sig.direction, tp, leg_entry):
                leg.valid = False
                leg.skip_reason = "tp already passed at entry"
            elif min_stop_distance is not None and abs(tp - leg_entry) < min_stop_distance:
                leg.valid = False
                leg.skip_reason = "tp within broker min distance"
            plan.legs.append(leg)
    return plan
