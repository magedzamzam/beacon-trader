"""Fanout planner — turns one ParsedSignal into N concrete legs.

legs = (distinct entry levels) x (tp_strategy tokens)

`tp_strategy` is a per-source template like "tp1, tp1, tp2, tp3": each token is
one leg targeting that TP index. Repeating tp1 is how a source weights the
high-probability target. A single entry with "tp1,tp2,tp3" => 3 legs; a range
entry (entry_from != entry_to) doubles that to 6, matching the spec example.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

from ..parsing.models import ParsedSignal

_TOKEN_RE = re.compile(r"tp\s*(\d+)", re.I)


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


def _parse_template(tp_strategy: str, n_tps: int) -> List[int]:
    tokens = [int(m) for m in _TOKEN_RE.findall(tp_strategy or "")]
    if not tokens:                       # default: one leg per available TP
        tokens = list(range(1, n_tps + 1))
    return tokens


def build_plan(sig: ParsedSignal, *, tp_strategy: str, order_position_type: str,
               current_price: Decimal,
               min_stop_distance: Optional[Decimal] = None) -> FanoutPlan:
    order_type = (order_position_type or "MARKET").upper()
    entries = [sig.entry_from]
    if sig.entry_to != sig.entry_from:
        entries.append(sig.entry_to)

    tokens = _parse_template(tp_strategy, len(sig.tps))
    plan = FanoutPlan(symbol=sig.symbol, direction=sig.direction, order_type=order_type)

    for entry in entries:
        # MARKET fills at the live price; LIMIT rests at the signalled level.
        leg_entry = current_price if order_type == "MARKET" else entry
        for tok in tokens:
            if tok < 1 or tok > len(sig.tps):
                continue
            tp = sig.tps[tok - 1]
            leg = PlannedLeg(side=sig.direction, entry=leg_entry, tp=tp,
                             sl=sig.sl, tp_index=tok, order_type=order_type)
            # Broker minimum stop/limit distance: drop only the offending leg.
            if min_stop_distance is not None:
                if abs(tp - leg_entry) < min_stop_distance:
                    leg.valid = False
                    leg.skip_reason = "tp within broker min distance"
            plan.legs.append(leg)
    return plan
