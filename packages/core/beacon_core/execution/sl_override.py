"""Replace a signal's STOP with one of our own, before the plan is built (#249).

The stop arrives from the channel and, until now, nothing could move it. This is
the one lever that changes the SIGNAL rather than how we act on it, and it exists
because the two distributions that justify it barely overlap: measured on the
excursion ladder 2026-08-19, TFXC's winners dip a median of $1.12 before reaching
TP1 while its losers dip $17.51, against the $12 stop the channel sends.

APPLIED BEFORE SIZING, ON PURPOSE. `lot = risk_cash / |entry - sl|`, so a tighter
stop buys a LARGER lot at the SAME cash risk. That is the mechanism under test,
not a side effect to be corrected for: the trade risks what it always risked, and
either stops out sooner or reaches its TP with more size on. Applied after the
plan is built, the lot would be sized against the channel's stop while the order
carried ours — the one ordering that is definitely wrong.

ANCHORED ON `entry_to`, THE FAR EDGE — NOT `entry_from`.
Two thirds of the book (884 of 1325 signals to 2026-08-20) is a zone entry, median
width $5.00, and the planner fans a zone onto BOTH edges. `entry_from` is the near
edge; a stop measured from it at any distance narrower than the zone lands on the
wrong side of the far edge, and `build_plan` then drops every leg resting there as
"sl on wrong side of entry" (planner.py:301). The feature would appear to do
nothing on the majority of signals, and would do it silently. `entry_to` is
already the edge `validate_signal` measures the stop against (planner.py:170), and
it is the only anchor that yields a placeable stop on every leg of the fanout. For
a single-level signal the two are the same number.

COPY-ON-WRITE, ALWAYS. The executor builds ONE ParsedSignal per signal and fans it
across every account (`services/executor/main.py:315`). Mutating it in place would
push one account's stop onto the arms that never asked for it — a silent money bug
in a system whose entire purpose is comparing those arms. `apply` returns a NEW
signal and never touches the caller's object.

Shared with `services/replay` (#248), which asks the same question offline, so a
backtested stop distance and a live one cannot drift apart.

PURE — stdlib only.
"""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
from typing import Optional

MODE_FIXED = "fixed"      # always set the stop to this distance
MODE_CAP = "cap"          # only tighten; leave an already-tighter stop alone
MODES = (MODE_FIXED, MODE_CAP)

# What `apply` did, for the audit event. A stop override that quietly fell back to
# the channel's stop on half the book is an override whose result means nothing,
# so every outcome is named and counted rather than collapsed into a bool.
APPLIED = "applied"
ALREADY_TIGHTER = "already_tighter"
BELOW_BROKER_MIN = "below_broker_minimum"
NO_GEOMETRY = "skipped_no_geometry"


def resolve_distance(entry_policy: Optional[dict]) -> Optional[Decimal]:
    """The configured stop distance for this (account, source), or None.

    None and 0 both mean "leave the channel's stop alone" — an operator clearing
    the field sends an empty string, which must read as OFF and never as a stop
    of zero distance."""
    if not entry_policy:
        return None
    raw = entry_policy.get("sl_distance")
    if raw is None or raw == "":
        return None
    try:
        dist = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return dist if dist > 0 else None


def new_stop(entry, direction: str, distance: Decimal) -> Decimal:
    """The stop `distance` price units on the losing side of `entry`."""
    entry = Decimal(str(entry))
    distance = Decimal(str(distance))
    return entry - distance if direction == "BUY" else entry + distance


def apply(signal, distance, *, mode: str = MODE_FIXED,
          min_stop_distance=None) -> tuple:
    """`(signal, note)` — the signal to plan, and what happened to it.

    Returns the ORIGINAL object and `None` when no override is configured, so the
    ordinary path does not pay for a copy it will not use.

    `min_stop_distance` is the broker's floor. A stop inside it is REJECTED at
    placement, so the override refuses rather than sending an order that cannot
    be filled — and the refusal is named, not swallowed.
    """
    if distance is None:
        return signal, None
    try:
        want = Decimal(str(distance))
    except (InvalidOperation, TypeError, ValueError):
        return signal, None
    if want <= 0:
        return signal, None

    direction = getattr(signal, "direction", None)
    # The far edge — see the module docstring. A signal carrying only one entry
    # level has entry_from == entry_to by definition, so falling back to it is
    # the same number, not a different rule.
    anchor = getattr(signal, "entry_to", None)
    if anchor is None:
        anchor = getattr(signal, "entry_from", None)
    sl = getattr(signal, "sl", None)
    if anchor is None or sl is None or direction not in ("BUY", "SELL"):
        return signal, NO_GEOMETRY

    if min_stop_distance is not None and want < Decimal(str(min_stop_distance)):
        return signal, BELOW_BROKER_MIN

    have = abs(Decimal(str(anchor)) - Decimal(str(sl)))
    if mode == MODE_CAP and have <= want:
        return signal, ALREADY_TIGHTER

    out = copy.copy(signal)                          # never mutate the shared signal
    out.sl = new_stop(anchor, direction, want)
    return out, APPLIED
