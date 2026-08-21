"""Replace a signal's STOP with one of our own choosing, before it is planned.

THE GEOMETRY LIVES IN `beacon_core.execution.sl_override` (#249). It used to live
here, because when this was written (#248) no live setting could move a channel's
stop and a research-only lever deserved to say so in its own name. Live can do it
now — it is `entry_policy.sl_distance`, set per (account, source) on the
Strategies page — so keeping a second copy of the arithmetic here would let a
backtested stop distance and a live one drift apart, which is the one thing a
backtest of this lever must never do. What remains in this module is the part
that is genuinely replay's: parsing a variant's `signal_overrides` block and
deciding which sources it targets.

CORRECTION TO #248's RESULTS ON ZONE SIGNALS. This module anchored the new stop
on `entry_from`, the NEAR edge. Two thirds of the book is a zone entry (median
width $5.00), and the planner fans a zone onto both edges — so any distance
narrower than the zone put the stop on the wrong side of the far edge, and
`build_plan` dropped those legs as "sl on wrong side of entry". A #248 run over
zone signals was therefore measuring a partially-dropped fanout, not a tighter
stop. The core module anchors on `entry_to`, the edge `validate_signal` already
measures the stop against, which is placeable on every leg. Re-run any #248
comparison that informed a stop-distance decision.

WHAT IT IS FOR. Measured 2026-08-19 on the excursion ladder: TFXC's winners dip a
median of $1.12 before reaching TP1 and a quarter never dip at all, while its
losers dip a median of $17.51. Those two distributions barely overlap, which is
the shape that makes a tighter stop worth testing rather than merely plausible.

WHAT IT DOES NOT DO. It does not touch the TPs, the entry, or the direction, and
it never widens a stop unless asked to. The sizing engine downstream is the real
one, so a tighter stop produces a LARGER lot at the same risk — which is the
mechanism under test, not a side effect to be corrected for.

PURE — stdlib + beacon_core.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from beacon_core.execution import sl_override as SLO

# The spec's mode names are part of stored run configs — keep them, and map them
# onto the core modes rather than renaming a field that is already on disk.
MODE_FIXED = "fixed_points"
MODE_CAP = "cap_points"          # only tighten; leave a tighter stop alone
MODES = (MODE_FIXED, MODE_CAP)
_CORE_MODE = {MODE_FIXED: SLO.MODE_FIXED, MODE_CAP: SLO.MODE_CAP}


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
    return SLO.new_stop(entry, direction, distance)


def apply(signal, source_id, spec: Optional[dict],
          *, min_stop_distance: Optional[Decimal] = None) -> tuple:
    """`(signal, note)` — the signal to plan, and what happened to it.

    Returns the ORIGINAL object untouched when the override does not apply, so a
    run with a source filter is not quietly deep-copying the whole book. The
    notes are the core module's, so a replay run and a live account describe the
    same outcome with the same word."""
    if not applies_to(spec, source_id):
        return signal, None
    return SLO.apply(signal, spec["sl_distance_points"],
                     mode=_CORE_MODE.get(spec["mode"], SLO.MODE_FIXED),
                     min_stop_distance=min_stop_distance)


def summary(notes: List[Optional[str]]) -> Dict[str, Any]:
    """What the override actually did across a run — reported, never assumed."""
    out: Dict[str, int] = {}
    for n in notes:
        if n:
            out[n] = out.get(n, 0) + 1
    return out
