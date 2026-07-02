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


def _norm(s: str) -> str:
    return (s or "").upper().replace("/", "").replace(".", "").replace(" ", "")


async def _mid_for_pair(adapter: BrokerAdapter, pair: str) -> Optional[Decimal]:
    """Find a broker FX market that genuinely IS `pair` and return its mid.

    Only accepts an exact symbol/name match, or the pair appearing whole inside
    the epic (e.g. CS.D.AEDUSD.CFD.IP). It never falls back to an arbitrary
    search result — a wrong instrument would silently mis-size every order (the
    cause of the 12%-instead-of-5% risk). No confident match -> None, and the
    caller skips the account rather than guessing."""
    try:
        matches = await adapter.search_instrument(pair)
    except Exception as exc:
        log.warning("fx search '%s' failed: %s", pair, exc)
        return None
    epic = None
    for m in matches:                       # exact symbol/name first
        if _norm(m.broker_symbol) == pair or _norm(getattr(m, "name", "")) == pair:
            epic = m.broker_symbol
            break
    if epic is None:                        # pair embedded whole in the epic/name
        for m in matches:
            if pair in _norm(m.broker_symbol) or pair in _norm(getattr(m, "name", "")):
                epic = m.broker_symbol
                break
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


def _from_overrides(overrides: dict, frm: str, to: str) -> Optional[Decimal]:
    """Manual rates configured in settings (key 'fx'), e.g. {'AEDUSD': '0.2723'}.
    Accepts the direct pair or its inverse."""
    if not overrides:
        return None
    direct = overrides.get(frm + to) or overrides.get(f"{frm}{to}".upper())
    if direct:
        try:
            d = Decimal(str(direct))
            if d > 0:
                return d
        except Exception:
            pass
    inv = overrides.get(to + frm) or overrides.get(f"{to}{frm}".upper())
    if inv:
        try:
            d = Decimal(str(inv))
            if d > 0:
                return Decimal(1) / d
        except Exception:
            pass
    return None


async def factor(adapter: BrokerAdapter, from_ccy: str, to_ccy: str,
                 overrides: Optional[dict] = None) -> Decimal:
    frm = (from_ccy or "").upper()
    to = (to_ccy or "").upper()
    if not frm or not to or frm == to:
        return Decimal(1)

    # Manual override wins — lets AED (or any) accounts size correctly when the
    # broker has no directly-searchable FX market for the pair.
    ov = _from_overrides(overrides or {}, frm, to)
    if ov is not None:
        return ov

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
