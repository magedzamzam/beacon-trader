"""Recovering a leg the broker refused, instead of dropping its size (#221).

A trade's TP ladder is placed leg-by-leg against a moving market. Two broker
refusals are routinely recoverable and were being discarded, taking the leg's
size with them — 49 legs and 954.93 lots over six weeks, silently, because a
rejected leg only ever wrote a `rejected` row and no size was recorded anywhere:

  * `error.validation.limit.price` — a resting LIMIT priced through the market.
    The SAME level was accepted on earlier rungs of the same ladder and refused
    on a later one, so the price was never wrong; the market simply arrived
    first. This is #140's mechanism on the ordinary entry path.
  * `error.invalid.takeprofit.{min,max}value: <bound>` — the take-profit is
    outside the broker's allowed distance, and the error NAMES the allowed
    value. Discarding a message that tells us the answer is the avoidable part.

Pure by design: the parsing and the decision live here so they are testable on a
bare box, and the executor keeps only the placement call (the repo's convention,
and what #198/#202 did for broker truth).

THE SAFETY ARGUMENT for the crossed-LIMIT retry, which is the money-touching
half. A LIMIT is refused as "at market" only once price has reached or passed
the level. Filling at market from there is at-or-BETTER than the level in both
directions — a SELL rests above the market, so a market fill is >= the level;
a BUY rests below, so a market fill is <= it. Entry improves, planned SL and TP
are unchanged, and therefore **planned risk-per-trade cannot increase**. It is
the identical argument reviewed and accepted for the staged runner in #140, and
this module exists so both paths state it once.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Optional

# What we can do about a refusal.
RETRY_AS_MARKET = "market"      # crossed LIMIT -> take it at market
RETRY_CLAMP_TP = "clamp_tp"     # TP outside the broker's band -> use its bound
NO_RETRY = None

_LIMIT_CROSSED = "error.validation.limit.price"
# `error.invalid.takeprofit.maxvalue: 4083.92`
_TP_BOUND_RE = re.compile(
    r"error\.invalid\.takeprofit\.(?P<which>max|min)value\s*:\s*(?P<bound>-?\d+(?:\.\d+)?)")


def classify_broker_error(message) -> dict:
    """Turn a broker error string into a decision.

    Returns `{"kind", "bound"}` where `kind` is one of the RETRY_* constants or
    None. Unrecognised text is NO_RETRY — an error we do not understand must not
    become an order we did not intend."""
    text = "" if message is None else str(message)
    if _LIMIT_CROSSED in text:
        return {"kind": RETRY_AS_MARKET, "bound": None}
    m = _TP_BOUND_RE.search(text)
    if m:
        try:
            return {"kind": RETRY_CLAMP_TP, "bound": Decimal(m.group("bound"))}
        except (InvalidOperation, ValueError):
            return {"kind": NO_RETRY, "bound": None}
    return {"kind": NO_RETRY, "bound": None}


def _dec(v) -> Optional[Decimal]:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def retry_plan(message, *, side_buy: bool, order_type: str,
               entry=None, take_profit=None) -> Optional[dict]:
    """How to re-place a refused leg once, or None to leave it rejected.

    `{"action": RETRY_AS_MARKET, "order_type": "MARKET", "limit_price": None}`
    or `{"action": RETRY_CLAMP_TP, "take_profit": Decimal}`.

    Every branch fails CLOSED: anything unrecognised, unparseable, or that would
    produce a nonsensical order returns None and the leg stays rejected. A
    dropped leg is a known, survivable loss of size; an order we did not mean to
    place is not."""
    decision = classify_broker_error(message)
    kind = decision["kind"]

    if kind == RETRY_AS_MARKET:
        # Only a working order can be "priced through the market" — retrying a
        # MARKET order as MARKET would just repeat whatever really failed.
        if str(order_type).upper() == "MARKET":
            return None
        return {"action": RETRY_AS_MARKET, "order_type": "MARKET", "limit_price": None}

    if kind == RETRY_CLAMP_TP:
        bound, e = decision["bound"], _dec(entry)
        if bound is None:
            return None
        # The clamped target must still be a target: on the profitable side of
        # the entry it is attached to. Without this guard a mis-parsed or
        # wrong-sided bound would flip the take-profit through the entry and
        # turn the leg into an instant loss.
        if e is not None:
            if side_buy and not bound > e:
                return None
            if not side_buy and not bound < e:
                return None
        # Clamping must only ever move the target CLOSER; the broker is stating
        # a ceiling on distance, so a "clamp" that lengthened the target would
        # be us inventing a more ambitious trade than the channel called.
        tp = _dec(take_profit)
        if tp is not None:
            if side_buy and bound > tp:
                return None
            if not side_buy and bound < tp:
                return None
        return {"action": RETRY_CLAMP_TP, "take_profit": bound}

    return None


def rejection_event(leg, *, intended_lot, error, retried_as=None, recovered=False) -> dict:
    """The payload that makes a lost leg VISIBLE (#221).

    The size is the point. A leg row marked `rejected` says something failed; it
    never said how much exposure the trade therefore never carried, which is why
    six weeks of this went unnoticed while the de-lever instrument (#188) read
    the shortfall as an arm choosing to risk less."""
    return {
        "leg_id": getattr(leg, "id", None),
        "tp_index": getattr(leg, "tp_index", None),
        "order_type": getattr(leg, "order_type", None),
        "intended_lot": str(intended_lot) if intended_lot is not None else None,
        "error": str(error)[:300] if error is not None else None,
        "retried_as": retried_as,
        "recovered": bool(recovered),
    }
