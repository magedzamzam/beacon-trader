"""How a closed leg got its money, and whether that money may be believed (#234).

A leg's `realized_pl` is only as good as the broker transaction it came from,
and there are three very different ways it can arrive. Until now they were
indistinguishable once written, so a figure derived from the wrong position's
close looked exactly like one taken from the leg's own:

  `exact`        the closing transaction matched THIS leg's position dealId.
                 The only auditable basis, and the only one analysis may sum.
  `unattributed` no such transaction. The close is real -- the position is gone
                 from the broker -- but which transaction settled it is unknown,
                 so there is no honest figure to record.
  `heuristic`    no broker transaction at all; the money is `distance x lot x
                 value-per-point`, computed by us. Rare (3 legs historically),
                 and never to be confused with a settled amount.

WHY THIS EXISTS. `_close_leg` used to fall back to matching a transaction by
INSTRUMENT when a leg had no position ref -- on a single-instrument bot that is
"any unclaimed XAUUSD close in the last six hours" -- then copy that
transaction's P&L onto the leg AND stamp the leg with the foreign deal id. 506
legs ended up holding another position's money to the cent, worth 47,524.5 AED,
across legs whose lot sizes differ by up to 3.6x. #9 diagnosed this exactly and
prescribed the fix; two of its four steps shipped, and 96% of the damage
happened afterwards.

The rule here is the one #9 asked for: **a transaction that is not this leg's
may decide that the leg CLOSED, but never how much it made.** Existence and
amount are separate questions, and only the second one needs the dealId.
"""
from __future__ import annotations

from typing import Optional

ATTR_EXACT = "exact"
ATTR_UNATTRIBUTED = "unattributed"
ATTR_HEURISTIC = "heuristic"

# The historical rows, marked by the startup backfill rather than by this code
# path. Kept distinct from `unattributed` because these carry a NUMBER that is
# positively known to be wrong (it is another leg's, to the cent), where an
# `unattributed` leg simply carries nothing.
ATTR_DUPLICATE = "duplicate"

ALL = (ATTR_EXACT, ATTR_UNATTRIBUTED, ATTR_HEURISTIC, ATTR_DUPLICATE)

# What a per-leg statistic may sum. Deliberately a whitelist: a basis added
# later is excluded until somebody decides it belongs, which is the safe
# direction for a money figure.
AUDITABLE = (ATTR_EXACT,)


def is_exact_match(txn: Optional[dict], position_ref) -> bool:
    """Did this transaction settle THIS leg's position?

    String-compared because the two sides arrive from different places (broker
    JSON and our column) and one of them is occasionally an int."""
    if txn is None or not position_ref:
        return False
    return str(txn.get("deal_id") or "") == str(position_ref)


def classify(txn: Optional[dict], position_ref) -> str:
    """The basis a close about to be written should be stamped with."""
    if is_exact_match(txn, position_ref):
        return ATTR_EXACT
    if txn is not None:
        # A transaction was found, but not this leg's. It is evidence the
        # position is gone; it is not evidence of what the position made.
        return ATTR_UNATTRIBUTED
    return ATTR_HEURISTIC


def realized_pl_for(basis: str, *, broker_pl, heuristic_pl):
    """The money to store for a close on this basis, or None to store nothing.

    `unattributed` returns None ON PURPOSE, and it is the whole fix: the
    alternatives are to copy the foreign transaction's amount (what produced
    the 47.5k) or to substitute the price-derived estimate (which would dress
    an unknown up as a measurement, and read as settled money downstream). A
    leg we cannot price is not a leg that lost 829.42."""
    if basis == ATTR_EXACT:
        return broker_pl
    if basis == ATTR_HEURISTIC:
        return heuristic_pl
    return None


def may_adopt_position_ref(basis: str) -> bool:
    """Whether the matched transaction's dealId may be written onto the leg.

    Only ever for an exact match -- which is a tautology, and that is the
    point. Adopting a heuristically-matched dealId is what made the defect
    permanent: the leg then carried another position's identity, so every later
    query saw a well-referenced leg and the error could not be found again
    without comparing lot sizes."""
    return basis == ATTR_EXACT


def is_auditable(basis: Optional[str]) -> bool:
    """Whether a per-leg money figure on this basis may enter a statistic.

    NULL reads as auditable: it is what every row predating the column carries
    until the backfill classifies it, and treating unclassified history as
    excluded would silently empty every existing report."""
    return basis is None or basis in AUDITABLE


def split_by_basis(legs) -> dict:
    """`{basis: [leg, ...]}` for a set of leg-ish rows — so a report can SAY how
    much of the book it dropped rather than quietly dropping it."""
    out: dict = {}
    for leg in legs or ():
        basis = getattr(leg, "pl_attribution", None) if not isinstance(leg, dict) \
            else leg.get("pl_attribution")
        out.setdefault(basis or ATTR_EXACT, []).append(leg)
    return out
