"""Simulated order lifecycle: one leg, one bar at a time (#169).

The unit of the harness is a LEG, mirroring `db.models.Leg` — a single
(entry, tp) pair sharing the trade's stop. This module owns exactly two
questions and nothing else:

    * did a working order fill inside this bar, and at what price?
    * did an open position close inside this bar, and how?

TWO CONSERVATIVE CHOICES, both counted and reported rather than assumed away:

1. **Same-bar TP/SL is scored as the STOP.** A 1m bar that touches both cannot
   say which came first. Guessing "TP, obviously" is how a backtest manufactures
   an edge, so the stop wins and `same_bar_ambiguous` is incremented. The count
   is a headline caveat on every result, not a footnote (#169 §3).
2. **A LIMIT fills AT its level, never better.** A bar that gaps through a
   resting BUY limit would in reality fill at the better open price; crediting
   that improvement is free money the harness cannot verify, so it is not
   credited. Slippage, where configured, is applied ADVERSELY only.
   That refusal is not free — it pushes `fill_adverse` positive on its own — so
   since #190 it is MEASURED per fill as `gap_at_open` and subtracted out before
   any of that number is attributed to the candle feed's spread.

Slippage is a per-fill price penalty in instrument points, applied to fills that
cross the spread on their own initiative — MARKET entries, STOP entries, and
stop-loss exits. A LIMIT entry and a take-profit exit are passive: they are hit
by the market at a level that was already resting, so they take no slippage. The
default is 0.0 — an explicit modelling choice the operator sets, not a number
the harness invents.

PURE — stdlib only. Every predicate comes from `bars.py`, so the sided
semantics live in exactly one place.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from . import bars as B

# --- leg statuses (mirror db.models.Leg.status) --------------------------------
PENDING = "pending"       # planned but not yet deployed (a staged runner/reclaim)
WORKING = "working"       # order resting at the (simulated) broker
OPEN = "open"             # filled; a position exists
CLOSED = "closed"
EXPIRED = "expired"       # TTL elapsed while still working
CANCELLED = "cancelled"   # retired by cancel_pending_on_stop
SKIPPED = "skipped"       # planner/sizing dropped it before it was ever placed

TERMINAL = (CLOSED, EXPIRED, CANCELLED, SKIPPED)

# --- outcomes (mirror db.models.Leg.outcome + report._RESOLVED_OUTCOMES) -------
TP_HIT = "tp_hit"
SL_HIT = "sl_hit"
BREAKEVEN = "breakeven"
EXPIRED_OUT = "expired"
HORIZON = "horizon"       # replay-only: still open when the horizon ran out

# How close a ratcheted stop must sit to the entry for its hit to be labelled
# `breakeven` rather than `sl_hit`. Mirrors the monitor's tolerance test
# (`_classify_outcome`): the ratchet targets `entry` exactly, so anything beyond
# float noise means the stop was moved somewhere else.
BE_TOLERANCE = 1e-9


@dataclass
class SimLeg:
    """One simulated leg. `entry` is the PLANNED level (what sizing used);
    `fill_price` is what it actually got. Both are kept because R is measured
    against planned risk, exactly as `geometry_ab_rollup` does live."""
    tp_index: int
    order_type: str                     # MARKET | LIMIT | STOP
    entry: float
    tp: float
    sl: float                           # current (ratcheted) stop
    initial_sl: float                   # immutable original — the R denominator (#109)
    lot: Decimal
    risk_cash: Decimal                  # planned worst-case loss, account ccy
    tranche: Optional[str] = None       # toe_in | runner | reclaim (staged only)
    trigger: Optional[float] = None     # STOP trigger level

    status: str = WORKING
    placed_at: Optional[dt.datetime] = None
    fill_price: Optional[float] = None
    filled_at: Optional[dt.datetime] = None
    close_price: Optional[float] = None
    closed_at: Optional[dt.datetime] = None
    realized_pl: Optional[Decimal] = None
    outcome: Optional[str] = None
    sl_moved: bool = False
    same_bar_ambiguous: bool = False
    events: list = field(default_factory=list)   # (ts, kind) audit, deterministic

    # --- under-fill diagnostic (#185) ----------------------------------------
    # The CLOSEST the fillable side ever came to this order's level while it was
    # working, in points; None for a MARKET order (there is no gap to close).
    # Positive means it never reached.
    #
    # It exists because "the simulator expired it, live filled it" is 47% of the
    # gate's disagreements and has two incompatible explanations — a candle feed
    # whose spread prints wider than Capital.com's was, or a TTL/window bug. The
    # DISTRIBUTION of these numbers tells them apart; nothing else does, and the
    # gate passing does not.
    closest_approach: Optional[float] = None
    # True when the TTL retired this order on a bar it WOULD have filled in.
    # `_expire_working` runs before `_fill_working`, which is the cheapest of the
    # candidate causes to eliminate — so it is COUNTED rather than argued about.
    expired_on_fillable_bar: bool = False

    # --- fill-model decomposition (#190) --------------------------------------
    # What choice 2 below cost (or saved) on THIS fill, adverse-signed. Positive
    # = a LIMIT that declined a gap-open improvement; negative = a STOP filled at
    # its trigger on a bar that opened beyond it. None when the bar did not open
    # through the level, so live would have filled where the simulator did.
    #
    # It exists so `fill_adverse` can be SPLIT: a deliberate refusal to credit
    # price improvement produces a positive mean on its own, and attributing that
    # to the candle feed's spread would charge spread for the harness's own rule.
    gap_at_open: Optional[float] = None

    # --- helpers --------------------------------------------------------------
    @property
    def is_working(self) -> bool:
        return self.status == WORKING

    @property
    def is_open(self) -> bool:
        return self.status == OPEN

    @property
    def is_pending(self) -> bool:
        """Planned but undeployed — a staged runner or reclaim tranche waiting on
        the DECIDE engine. It owns nothing at the broker."""
        return self.status == PENDING

    @property
    def is_live(self) -> bool:
        """Still capable of costing money: waiting to deploy, resting, or open."""
        return self.status in (PENDING, WORKING, OPEN)

    def deploy(self, ts, order_type: str, level: float,
               trigger: Optional[float] = None) -> None:
        """A staged tranche's order goes to the (simulated) broker. Its TTL clock
        starts HERE, not at signal time — a tranche deploys late by design and
        `staging.deployed_ttl_minutes` is what bounds the extra window (#158)."""
        self.status = WORKING
        self.order_type = order_type
        self.entry = level
        self.trigger = trigger
        self.placed_at = ts
        self.note(ts, "deployed")

    def note(self, ts, kind: str) -> None:
        self.events.append((ts, kind))


def level_of(leg: SimLeg) -> float:
    """The price this order is waiting for: a STOP's trigger, else its entry."""
    if leg.order_type == "STOP" and leg.trigger is not None:
        return leg.trigger
    return leg.entry


def track_approach(leg: SimLeg, direction: str, bar: B.Bar) -> None:
    """Record how close this bar came to filling a resting order (#185).

    Called on every bar a leg spends WORKING, including the one it fills on — the
    minimum is what matters, and an order that filled has a shortfall of <= 0 by
    definition, which is what makes the two populations comparable."""
    if not leg.is_working:
        return
    gap = B.entry_shortfall(direction, leg.order_type, level_of(leg), bar)
    if gap is None:
        return
    if leg.closest_approach is None or gap < leg.closest_approach:
        leg.closest_approach = gap


def would_fill(leg: SimLeg, direction: str, bar: B.Bar) -> bool:
    """Would this resting order fill inside `bar`? The predicate `try_fill` uses,
    without the side effect — so the TTL can ask "was this bar fillable?" before
    retiring the order, and the answer is counted rather than assumed."""
    if not leg.is_working:
        return False
    if leg.order_type == "MARKET":
        return True
    if leg.order_type == "LIMIT":
        return B.limit_touched(direction, leg.entry, bar)
    if leg.order_type == "STOP":
        return B.stop_triggered(direction, level_of(leg), bar)
    return False


def try_fill(leg: SimLeg, direction: str, bar: B.Bar, *,
             slippage_points: float = 0.0) -> bool:
    """Fill `leg` inside `bar` if its order type says so. Returns True on a fill.

    MARKET fills at the bar's open on the side we pay, plus slippage. LIMIT fills
    at its level when the correct side reaches it (passive — no slippage). STOP
    fills at its trigger plus slippage: a stop entry crosses the spread by
    construction and is the fill most exposed to a fast market.
    """
    if not leg.is_working:
        return False
    ot = leg.order_type
    if ot == "MARKET":
        px = B.entry_price_now(direction, bar) + B.adverse(direction, slippage_points)
    elif ot == "LIMIT":
        if not B.limit_touched(direction, leg.entry, bar):
            return False
        px = leg.entry
        leg.gap_at_open = B.gap_at_open(direction, ot, leg.entry, bar)
    elif ot == "STOP":
        level = leg.trigger if leg.trigger is not None else leg.entry
        if not B.stop_triggered(direction, level, bar):
            return False
        px = level + B.adverse(direction, slippage_points)
        leg.gap_at_open = B.gap_at_open(direction, ot, level, bar)
    else:
        return False
    leg.status = OPEN
    leg.fill_price = px
    leg.filled_at = bar.ts
    leg.note(bar.ts, "filled")
    return True


def resolve_exit(leg: SimLeg, direction: str, bar: B.Bar, *,
                 slippage_points: float = 0.0) -> bool:
    """Close `leg` inside `bar` if its stop or target was touched. True on a close.

    Order of resolution is ADVERSE FIRST: the stop is tested before the target,
    and a bar touching both is scored as the stop with `same_bar_ambiguous` set.
    That is the whole conservatism of the simulator in three lines, so it is
    stated here rather than distributed across callers.
    """
    if not leg.is_open:
        return False
    sl_here = B.sl_touched(direction, leg.sl, bar)
    tp_here = B.tp_touched(direction, leg.tp, bar)
    if not (sl_here or tp_here):
        return False
    if sl_here:
        if tp_here:
            leg.same_bar_ambiguous = True
        px = leg.sl - B.adverse(direction, slippage_points)
        outcome = (BREAKEVEN if leg.sl_moved
                   and abs(leg.sl - (leg.fill_price if leg.fill_price is not None
                                     else leg.entry)) <= BE_TOLERANCE
                   else SL_HIT)
    else:
        px = leg.tp                                   # passive fill, no slippage
        outcome = TP_HIT
    close_leg(leg, direction, px, outcome, bar.ts)
    return True


def close_leg(leg: SimLeg, direction: str, price: float, outcome: str,
              ts, *, value_per_point: Decimal = Decimal("1"),
              fx_factor: Decimal = Decimal("1")) -> None:
    """Terminate an OPEN leg at `price`. P&L is left for `settle` so the money
    conversion happens in one place with the instrument's constants."""
    leg.status = CLOSED
    leg.close_price = price
    leg.closed_at = ts
    leg.outcome = outcome
    leg.note(ts, outcome)


def settle(leg: SimLeg, direction: str, *, value_per_point: Decimal,
           fx_factor: Decimal = Decimal("1")) -> Decimal:
    """Realized P&L in ACCOUNT currency for a closed leg, and store it.

        BUY:  (exit - entry) x lot x value_per_point / fx
        SELL: (entry - exit) x lot x value_per_point / fx

    `value_per_point` is in INSTRUMENT currency and `fx_factor` converts
    account -> instrument, exactly as `risk.sizing.size_legs` does — so a leg
    stopped at its original SL settles at precisely `-risk_cash` and R is 1.0 by
    construction rather than by rounding luck.
    """
    if leg.close_price is None or leg.fill_price is None or not leg.lot:
        leg.realized_pl = Decimal("0")
        return leg.realized_pl
    move = Decimal(str(leg.close_price)) - Decimal(str(leg.fill_price))
    if direction == B.SELL:
        move = -move
    pl = (move * leg.lot * value_per_point) / (fx_factor or Decimal("1"))
    leg.realized_pl = pl
    return pl


def expire(leg: SimLeg, ts, reason: str = EXPIRED_OUT) -> None:
    """Retire a still-WORKING order. It never held a position, so it has no P&L —
    an expired entry is a trade that did not happen, not a zero-P&L trade, and it
    must not dilute a variant's expectancy."""
    if not leg.is_working:
        return
    leg.status = EXPIRED
    leg.outcome = EXPIRED_OUT
    leg.closed_at = ts
    leg.note(ts, reason)


def cancel(leg: SimLeg, ts, reason: str) -> None:
    """`cancel_pending_on_stop` retirement (#161). Distinct from expiry so the
    two are countable apart: an expiry is the TTL doing its job, a cancel is the
    trade being over while an order was still resting."""
    if not leg.is_working:
        return
    leg.status = CANCELLED
    leg.outcome = None
    leg.closed_at = ts
    leg.note(ts, reason)


def close_at_horizon(leg: SimLeg, direction: str, bar: B.Bar) -> None:
    """The horizon ran out with the position still open. Marked-to-market at the
    close of the last bar on the side we would exit on, and labelled `horizon` —
    NOT `manual` and not a win. A run reports the horizon-capped count, because a
    variant that parks losers indefinitely would otherwise look patient rather
    than unresolved."""
    if not leg.is_open:
        return
    px = bar.close_bid if direction == B.BUY else bar.close_ask
    close_leg(leg, direction, px, HORIZON, bar.ts)
