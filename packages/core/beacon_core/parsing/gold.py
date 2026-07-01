"""Signal parser. Symbol-aware port of the original gold parser.

Gate (all four must be present):
    direction (BUY|SELL), a known symbol, a TP marker, an SL marker.

Numbers before the first TP/SL mention are candidate ENTRIES; numbers after are
SL + TPs. The symbol's price band (not a hardcoded 2000) decides what counts as
a price, so the same code works for Silver/FX once their SymbolSpec is enabled.

BUY : entries sorted DESC (higher = entry_from); risk sorted ASC (lowest = SL).
SELL: entries sorted ASC  (lower  = entry_from); risk sorted DESC (highest = SL).
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from .models import ParsedSignal
from .symbols import SymbolSpec, detect_symbol

_RE_DIR = re.compile(r"\b(BUY|SELL)\b", re.I)
_RE_TP = re.compile(r"\b(TP\d*|TAKE\s*PROFIT)\b", re.I)
_RE_SL = re.compile(r"\b(SL|STOP\s*LOSS|STOPLOSS)\b", re.I)
_RE_NUM = re.compile(r"\d+\.?\d*")
_RE_LIMIT = re.compile(r"\b(LIMIT|SELL\s*LIMIT|BUY\s*LIMIT)\b", re.I)
_RE_MARKET = re.compile(r"\b(NOW|MARKET|BUY\s*NOW|SELL\s*NOW)\b", re.I)

_SUPERSCRIPT = str.maketrans({
    "\u2070": "0", "\u00b9": "1", "\u00b2": "2", "\u00b3": "3", "\u2074": "4",
    "\u2075": "5", "\u2076": "6", "\u2077": "7", "\u2078": "8", "\u2079": "9",
})


def _fix(text: str) -> str:
    return text.upper().translate(_SUPERSCRIPT)


def _order_hint(text: str) -> Optional[str]:
    # LIMIT wins if both appear (an explicit "limit" is a stronger intent).
    if _RE_LIMIT.search(text):
        return "LIMIT"
    if _RE_MARKET.search(text):
        return "MARKET"
    return None


def parse(message: str, spec: Optional[SymbolSpec] = None) -> Optional[ParsedSignal]:
    text = _fix(message)
    spec = spec or detect_symbol(text)
    if spec is None:
        return None

    m_dir = _RE_DIR.search(text)
    m_tp = _RE_TP.search(text)
    m_sl = _RE_SL.search(text)
    if not (m_dir and m_tp and m_sl):
        return None

    direction = m_dir.group(1).upper()
    split = min(m_tp.start(), m_sl.start())
    entry_str, risk_str = text[:split], text[split:]

    def band_nums(s: str):
        out = []
        for n in _RE_NUM.findall(s):
            f = float(n)
            if spec.in_band(f):
                out.append(Decimal(n))
        return out

    entry_nums = band_nums(entry_str)
    risk_nums = band_nums(risk_str)
    if not entry_nums or len(risk_nums) < 2:
        return None

    if direction == "BUY":
        entry_sorted = sorted(entry_nums, reverse=True)
        risk_sorted = sorted(risk_nums)
    else:
        entry_sorted = sorted(entry_nums)
        risk_sorted = sorted(risk_nums, reverse=True)

    entry_from = entry_sorted[0]
    entry_to = entry_sorted[1] if len(entry_sorted) > 1 else entry_sorted[0]
    sl = risk_sorted[0]
    tps = risk_sorted[1:]

    # SL must sit on the correct side of the entry, else we grabbed junk.
    if direction == "BUY" and sl >= entry_to:
        return None
    if direction == "SELL" and sl <= entry_to:
        return None

    return ParsedSignal(
        symbol=spec.internal, direction=direction,
        entry_from=entry_from, entry_to=entry_to, sl=sl, tps=tps,
        order_type_hint=_order_hint(text), raw_text=message,
    )
