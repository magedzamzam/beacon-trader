from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional


@dataclass
class ParsedSignal:
    """Normalised signal, broker-agnostic. Prices are Decimal."""
    symbol: str                       # internal symbol, e.g. "XAUUSD"
    direction: str                    # "BUY" | "SELL"
    entry_from: Decimal
    entry_to: Decimal
    sl: Decimal
    tps: List[Decimal] = field(default_factory=list)
    order_type_hint: Optional[str] = None   # "MARKET" | "LIMIT" | None
    raw_text: str = ""

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_from": str(self.entry_from),
            "entry_to": str(self.entry_to),
            "sl": str(self.sl),
            "tps": [str(t) for t in self.tps],
            "order_type_hint": self.order_type_hint,
            "raw_text": self.raw_text,
        }
