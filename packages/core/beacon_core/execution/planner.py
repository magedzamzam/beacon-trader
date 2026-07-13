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

# Entry/planner config (DB-backed `planner` setting; edited from the platform,
# never hardcoded at a call site). The chase-tolerance default (#67) is the safe
# fallback: a MARKET-hint entry more than 0.25 × its stop-distance beyond the live
# price rests a LIMIT at the signalled level instead of chasing.
DEFAULT_PLANNER = {
    "honor_market_hint": True,        # master toggle for market-on-receipt (#25)
    "chase_tolerance_r": 0.25,        # max chase = this × |entry − SL|
    "chase_tolerance_atr": 0.0,       # or this × ATR (0 = disabled); larger of the two wins
    "beyond_tolerance": "limit",      # beyond tolerance: "limit" (rest) | "skip" (don't trade)
    "max_tp_distance_pct": 0.5,       # drop parse-artifact TPs this far from entry
}


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
    entry_decisions: List[dict] = field(default_factory=list)   # chase-guard audit (#67)

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


def _chase_gap(direction: str, price: Decimal, entry: Decimal) -> Decimal:
    """How far the live price sits BEYOND the signalled entry in the ADVERSE
    (chase) direction, i.e. how much worse than the level a market fill would be.
    0 when price is at or better than the entry — a BUY at/below its level, or a
    SELL at/above its level — where a market fill is fine, not a chase (#67)."""
    if direction == "BUY":
        return max(Decimal(0), price - entry)     # price above the buy level -> chasing up
    return max(Decimal(0), entry - price)          # price below the sell level -> chasing down


def _chase_tolerance(entry: Decimal, sl: Decimal, atr: Optional[Decimal],
                     tol_r: Decimal, tol_atr: Decimal) -> Decimal:
    """Max chase allowed, in price units — the larger of the R-based tolerance
    (a fraction of the entry->SL distance) and the ATR-based one. A 0 factor
    disables that term. The MARKET hint means 'enter now' only within this."""
    r = abs(entry - sl) * tol_r if tol_r and tol_r > 0 else Decimal(0)
    a = atr * tol_atr if (atr is not None and tol_atr and tol_atr > 0) else Decimal(0)
    return max(r, a)


def _tp_beyond(direction: str, tp: Decimal, entry: Decimal) -> bool:
    """TP must sit in the profit direction from the (actual) entry."""
    return tp > entry if direction == "BUY" else tp < entry


def _sl_protective(direction: str, sl: Decimal, entry: Decimal) -> bool:
    """SL must sit on the protective side of the (actual) entry."""
    return sl < entry if direction == "BUY" else sl > entry


def build_plan(sig: ParsedSignal, *, current_price: Decimal,
               candle_high: Optional[Decimal] = None,
               candle_low: Optional[Decimal] = None,
               min_stop_distance: Optional[Decimal] = None,
               max_tp_distance_pct: Optional[Decimal] = None,
               honor_market_hint: bool = True,
               chase_tolerance_r: Decimal = Decimal("0.25"),
               chase_tolerance_atr: Decimal = Decimal("0"),
               beyond_tolerance: str = "limit",
               atr: Optional[Decimal] = None) -> FanoutPlan:
    """Per leg: if the current candle has already crossed an entry level, that leg
    is opened MARKET now (the price already touched it and may not rebound);
    otherwise it rests as a LIMIT at the signalled level. Decided per entry level.

    Exception — if the signal itself says "enter now" (`order_type_hint == MARKET`,
    e.g. "BUY NOW") and `honor_market_hint` is set, it may fill MARKET at the live
    price (#25) — but ONLY within a bounded chase distance (#67): a concrete entry
    the price hasn't reached is a LIMIT by construction. If the live price is more
    than `chase_tolerance` beyond the signalled entry (adverse direction), the leg
    rests a LIMIT at the entry (default) or is skipped (`beyond_tolerance="skip"`),
    never chased. Tolerance = max(chase_tolerance_r × |entry−SL|,
    chase_tolerance_atr × ATR).
    """
    entries = [sig.entry_from]
    if sig.entry_to != sig.entry_from:
        entries.append(sig.entry_to)

    market_hint = honor_market_hint and (sig.order_type_hint or "").upper() == "MARKET"
    decisions: List[dict] = []
    if market_hint:
        # Bounded market-on-receipt (#67). Per entry: MARKET only if at/through
        # the level or within the chase tolerance; else rest a LIMIT (or skip).
        _beyond = "skip" if beyond_tolerance == "skip" else "limit"
        order_plan = []          # (order_type, leg_entry)
        market_added = False
        for e in entries:
            gap = _chase_gap(sig.direction, current_price, e)
            tol = _chase_tolerance(e, sig.sl, atr, chase_tolerance_r, chase_tolerance_atr)
            if gap <= tol:
                # Near/at the level — collapse eligible entries into ONE market fill.
                if not market_added:
                    order_plan.append(("MARKET", current_price))
                    market_added = True
                decision = "market"
            elif _beyond == "skip":
                decision = "skip"                       # beyond tolerance -> don't trade this entry
            else:
                order_plan.append(("LIMIT", e))         # beyond tolerance -> rest at the level
                decision = "limit"
            decisions.append({"entry": str(e), "gap": str(gap), "tolerance": str(tol),
                              "decision": decision})
    else:
        # Fold the live price into the candle range so an in-progress touch counts.
        highs = [x for x in (candle_high, current_price) if x is not None]
        lows = [x for x in (candle_low, current_price) if x is not None]
        hi = max(highs) if highs else None
        lo = min(lows) if lows else None

        # Any already-crossed entry collapses into ONE market fill at the live
        # price; each entry still waiting rests as its own LIMIT.
        order_plan = []  # (order_type, leg_entry)
        if any(_entry_crossed(sig.direction, e, hi, lo) for e in entries):
            order_plan.append(("MARKET", current_price))
        for e in entries:
            if not _entry_crossed(sig.direction, e, hi, lo):
                order_plan.append(("LIMIT", e))

    plan = FanoutPlan(symbol=sig.symbol, direction=sig.direction, order_type="LIMIT",
                      entry_decisions=decisions)

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
            elif (max_tp_distance_pct is not None and leg_entry
                  and abs(tp - leg_entry) / abs(leg_entry) > max_tp_distance_pct):
                # A TP an implausible distance from entry (e.g. tp=1530 for gold
                # near 4180) is a parse artifact, not a real target — skip it.
                leg.valid = False
                pct = int(abs(tp - leg_entry) / abs(leg_entry) * 100)
                leg.skip_reason = f"tp implausibly far ({pct}% from entry)"
            plan.legs.append(leg)
    return plan
