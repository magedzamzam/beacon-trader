"""Symbol registry. A symbol's *price band* is what lets the parser tell a real
price from a pip-count or ratio. Gold is the only band wired for Phase 1, but
adding Silver / FX later is just another SymbolSpec — no parser change needed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional


@dataclass(frozen=True)
class SymbolSpec:
    internal: str            # canonical name used across Beacon
    aliases: tuple           # regex-alternation tokens matched in message text
    price_min: Decimal       # numbers below this are noise (pips, ratios)
    price_max: Decimal       # numbers above this are noise
    point: Decimal           # 1 "point" in price terms (gold: 1.0 -> $1 move)

    @property
    def alias_re(self) -> "re.Pattern":
        joined = "|".join(re.escape(a) for a in self.aliases)
        return re.compile(rf"\b({joined})\b", re.I)

    def in_band(self, n: float) -> bool:
        return float(self.price_min) <= n <= float(self.price_max)


# Phase 1: Gold only. Structure intentionally leaves room for the rest.
REGISTRY: List[SymbolSpec] = [
    SymbolSpec("XAUUSD", ("XAUUSD", "XAU/USD", "XAU", "GOLD"),
               Decimal("1500"), Decimal("6000"), Decimal("1")),
    # --- add later (bands illustrative, calibrate before enabling) ---------
    # SymbolSpec("XAGUSD", ("XAGUSD", "XAG/USD", "SILVER"),
    #            Decimal("10"), Decimal("120"), Decimal("0.01")),
    # SymbolSpec("USDJPY", ("USDJPY", "USD/JPY"),
    #            Decimal("80"), Decimal("250"), Decimal("0.01")),
    # SymbolSpec("EURUSD", ("EURUSD", "EUR/USD"),
    #            Decimal("0.8"), Decimal("1.6"), Decimal("0.0001")),
]


def detect_symbol(text: str) -> Optional[SymbolSpec]:
    for spec in REGISTRY:
        if spec.alias_re.search(text):
            return spec
    return None
