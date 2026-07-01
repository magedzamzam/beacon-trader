"""Currency conversion resolved live from the broker's own FX markets.

Gold is quoted in USD; an account may be in USD, AED, or anything else. To size
correctly we convert the risk budget from account currency into the instrument's
currency using a real rate — never a hardcoded constant.

factor(from_ccy, to_ccy) returns the multiplier m such that
    amount_in_to_ccy = amount_in_from_ccy * m

Strategy, in order:
  1. same currency            -> 1
  2. broker market FROM+TO     (price = TO per FROM)          -> m = price
  3. broker market TO+FROM     (price = FROM per TO, inverse) -> m = 1/price
  4. nothing found            -> FxUnavailable (caller must not guess)

Results are cached briefly so a burst of signals doesn't hammer the broker.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Dict, Optional, Tuple

from .base import BrokerAdapter
from ..logging import get_logger

log = get_logger("fx")

_CACHE: Dict[Tuple[str, str], Tuple[Decimal, float]] = {}
_TTL = 300.0  # seconds


class FxUnavailable(Exception):
    pass


def _cached(frm: str, to: str) -> Optional[Decimal]:
    hit = _CACHE.get((frm, to))
    if hit and (time.time() - hit[1]) < _TTL:
        return hit[0]
    return None


def _store(frm: str, to: str, rate: Decimal) -> None:
    _CACHE[(frm, to)] = (rate, time.time())


async def _mid_for_pair(adapter: BrokerAdapter, pair: str) -> Optional[Decimal]:
    """Find a broker FX market whose epic/symbol matches `pair` and return its mid."""
    try:
        matches = await adapter.search_instrument(pair)
    except Exception as exc:
        log.warning("fx search '%s' failed: %s", pair, exc)
        return None
    epic = None
    for m in matches:
        sym = (m.broker_symbol or "").upper().replace("/", "")
        if sym == pair:
            epic = m.broker_symbol
            break
    if epic is None and matches:            # fall back to the closest match
        epic = matches[0].broker_symbol
    if epic is None:
        return None
    try:
        q = await adapter.get_quote(epic)
    except Exception as exc:
        log.warning("fx quote '%s' failed: %s", epic, exc)
        return None
    if q.last_price is not None:
        return q.last_price
    if q.bid is not None and q.offer is not None:
        return (q.bid + q.offer) / Decimal(2)
    return None


async def factor(adapter: BrokerAdapter, from_ccy: str, to_ccy: str) -> Decimal:
    frm = (from_ccy or "").upper()
    to = (to_ccy or "").upper()
    if not frm or not to or frm == to:
        return Decimal(1)

    cached = _cached(frm, to)
    if cached is not None:
        return cached

    # direct: market FROM+TO priced as TO per FROM
    direct = await _mid_for_pair(adapter, frm + to)
    if direct and direct > 0:
        _store(frm, to, direct)
        return direct

    # inverse: market TO+FROM priced as FROM per TO
    inv = await _mid_for_pair(adapter, to + frm)
    if inv and inv > 0:
        rate = Decimal(1) / inv
        _store(frm, to, rate)
        return rate

    raise FxUnavailable(f"no broker FX market for {frm}->{to}")
