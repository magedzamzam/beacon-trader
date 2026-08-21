"""Replace a signal's STOP with one of our own choosing, before it is planned.

WHY THIS IS NOT AN `execution_strategies` FIELD. Every other replay variant
changes CONFIG — how we act on a signal. This changes the SIGNAL: the stop level
arrives from the channel, and no live setting can move it. Answering "what if
TFXC's stop were 5 points instead of the 12 he sends" therefore needs a lever
the config vocabulary does not contain, and inventing a fake `exit_policy` key
for it would imply live could do this. Live cannot. This is a research lever and
it says so in its own name.

WHAT IT IS FOR. Measured 2026-08-19 on the excursion ladder: TFXC's winners dip
a median of $1.12 before reaching TP1 and a quarter never dip at all, while its
losers dip a median of $17.51. Those two distributions barely overlap, which is
the shape that makes a tighter stop worth testing rather than merely plausible.

WHAT IT DOES NOT DO. It does not touch the TPs, the entry, or the direction, and
it never widens a stop unless asked to. The sizing engine downstream is the real
one, so a tighter stop produces a LARGER lot at the same risk — which is the
mechanism under test, not a side effect to be corrected for.

PURE — stdlib + beacon_core's models.
"""
from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any, Dict, List, Optional

MODE_FIXED = "fixed_points"
MODE_CAP = "cap_points"          # only tighten; leave a tighter stop alone
MODES = (MODE_FIXED, MODE_CAP)


class OverrideError(ValueError):
    """The spec is unusable. Raised, never swallowed: a research lever that
    silently did nothing would report the baseline as the treatment."""


def parse(spec: Optional[dict]) -> Optional[dict]:
    """Validate a `signal_overrides` block, or None when there is none."""
    if not spec:
        return None
    mode = str(spec.get("mode") or MODE_FIXED)
    if mode not in MODES:
        raise OverrideError("signal_overrides.mode must be one of %s, got %r"
                            % (", ".join(MODES), mode))
    pts = spec.get("sl_distance_points")
    if pts is None:
        raise OverrideError("signal_overrides needs `sl_distance_points`")
    try:
        pts = Decimal(str(pts))
    except Exception:
        raise OverrideError("sl_distance_points must be a number, got %r" % (pts,))
    if pts <= 0:
        raise OverrideError("sl_distance_points must be positive, got %s" % pts)
    srcs = spec.get("sources")
    if srcs is not None:
        try:
            srcs = [int(x) for x in srcs]
        except Exception:
            raise OverrideError("signal_overrides.sources must be integers")
        if not srcs:
            raise OverrideError("signal_overrides.sources is empty — omit it to "
                                "apply to every source, rather than to none")
    return {"mode": mode, "sl_distance_points": pts, "sources": srcs}


def applies_to(spec: Optional[dict], source_id) -> bool:
    if not spec:
        return False
    srcs = spec.get("sources")
    if srcs is None:
        return True
    try:
        return int(source_id) in srcs
    except (TypeError, ValueError):
        return False                 # an unattributed signal is never targeted


def new_stop(entry, direction: str, distance: Decimal) -> Decimal:
    """The stop `distance` points on the losing side of `entry`."""
    entry = Decimal(str(entry))
    return entry - distance if direction == "BUY" else entry + distance


def apply(signal, source_id, spec: Optional[dict],
          *, min_stop_distance: Optional[Decimal] = None) -> tuple:
    """`(signal, note)` — the signal to plan, and what happened to it.

    Returns the ORIGINAL object untouched when the override does not apply, so
    a run with a source filter is not quietly deep-copying the whole book.

    `min_stop_distance` is the broker's floor. A stop inside it would be
    REJECTED live, so the override refuses rather than modelling an order that
    could not be placed — and the refusal is counted, because a variant that
    silently fell back to the channel's stop on half the book is a variant
    whose result means nothing."""
    if not applies_to(spec, source_id):
        return signal, None
    entry = getattr(signal, "entry_from", None)
    sl = getattr(signal, "sl", None)
    direction = getattr(signal, "direction", None)
    if entry is None or sl is None or direction not in ("BUY", "SELL"):
        return signal, "skipped_no_geometry"

    want = spec["sl_distance_points"]
    have = abs(Decimal(str(entry)) - Decimal(str(sl)))
    if spec["mode"] == MODE_CAP and have <= want:
        return signal, "already_tighter"
    if min_stop_distance is not None and want < Decimal(str(min_stop_distance)):
        return signal, "below_broker_minimum"

    out = copy.copy(signal)
    out.sl = new_stop(entry, direction, want)
    return out, "applied"


def summary(notes: List[Optional[str]]) -> Dict[str, Any]:
    """What the override actually did across a run — reported, never assumed."""
    out: Dict[str, int] = {}
    for n in notes:
        if n:
            out[n] = out.get(n, 0) + 1
    return out
