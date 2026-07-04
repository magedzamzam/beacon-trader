"""Crypto microstructure from Binance USDT-perp public REST (free, no key).

Isolated + swappable: everything that reads funding / basis / order-book
imbalance goes through `fetch_micro`. To swap venues (e.g. Bybit) replace this
module. All numbers are Decimal.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import List, Optional

from ..config import get_settings
from ..logging import get_logger

# httpx is imported lazily inside fetch_micro so the pure helpers in this module
# (e.g. liquidation_proxy) stay unit-testable without the network stack.

log = get_logger("alpha.crypto")


def _dec(v) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


async def fetch_micro(binance_sym: str) -> Optional[dict]:
    """Funding rate, perp-spot basis (mark-index), and top-20 order-book
    imbalance (sum bidQty / sum askQty). Returns None on failure (fail-safe)."""
    import httpx
    base = get_settings().binance_fapi
    try:
        async with httpx.AsyncClient(base_url=base, timeout=15.0,
                                     headers={"User-Agent": "beacon-trader/1.0"}) as c:
            pi = (await c.get("/fapi/v1/premiumIndex", params={"symbol": binance_sym})).json()
            depth = (await c.get("/fapi/v1/depth", params={"symbol": binance_sym, "limit": 20})).json()
    except Exception as exc:
        log.warning("binance micro fetch failed for %s: %s", binance_sym, exc)
        return None

    mark = _dec(pi.get("markPrice"))
    index = _dec(pi.get("indexPrice"))
    basis = (mark - index) if (mark is not None and index is not None) else None

    def _side_qty(rows) -> Decimal:
        total = Decimal(0)
        for row in (rows or []):
            q = _dec(row[1]) if isinstance(row, (list, tuple)) and len(row) > 1 else None
            if q is not None:
                total += q
        return total

    bid_q = _side_qty(depth.get("bids"))
    ask_q = _side_qty(depth.get("asks"))
    ob_imbalance = (bid_q / ask_q) if ask_q > 0 else None

    return {
        "funding": _dec(pi.get("lastFundingRate")),
        "funding_predicted": _dec(pi.get("interestRate")),   # best-effort proxy
        "basis": basis,
        "ob_imbalance": ob_imbalance,
    }


def liquidation_proxy(candles: List[dict], *, k: Decimal = Decimal("3"),
                      m: int = 3, retrace: Decimal = Decimal("0.5")) -> bool:
    """Forced-move proxy when a venue exposes no public liquidation feed: the
    most recent bar's range exceeds k×ATR AND price has retraced >= `retrace`
    of that move within `m` bars. `candles` are recent 1m dicts with h/l/c
    (Decimal or number), oldest→newest.
    """
    if not candles or len(candles) < max(m + 1, 5):
        return False
    try:
        highs = [Decimal(str(c["h"])) for c in candles]
        lows = [Decimal(str(c["l"])) for c in candles]
        closes = [Decimal(str(c["c"])) for c in candles]
    except (KeyError, InvalidOperation, TypeError):
        return False

    trs = [highs[i] - lows[i] for i in range(len(candles))]
    atr = sum(trs) / Decimal(len(trs))
    if atr <= 0:
        return False

    move_idx = len(candles) - 1 - m
    if move_idx < 0:
        return False
    move_range = highs[move_idx] - lows[move_idx]
    if move_range < k * atr:
        return False

    # Retrace: did price come back at least `retrace` of the move within m bars?
    up = closes[move_idx] >= (highs[move_idx] + lows[move_idx]) / 2
    extreme = highs[move_idx] if up else lows[move_idx]
    target = extreme - move_range * retrace if up else extreme + move_range * retrace
    after = closes[move_idx + 1: move_idx + 1 + m]
    if up:
        return any(c <= target for c in after)
    return any(c >= target for c in after)
