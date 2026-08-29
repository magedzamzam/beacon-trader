"""BrokerAdapter — the unified gateway contract. Add a broker by implementing
this interface; nothing above the adapter layer changes."""
from __future__ import annotations

import abc
import datetime as dt
from decimal import Decimal
from typing import Dict, List, Optional

from .types import (
    AccountInfo, BrokerCandle, BrokerInstrument, BrokerOrder, BrokerPosition,
    BrokerQuote, ClosePositionResult, ModifyOrderRequest, ModifyPositionRequest,
    OrderStatus, PlaceOrderRequest,
)


class BrokerAdapter(abc.ABC):
    is_automated: bool = True

    def __init__(self, credentials=None, display_metadata=None, base_url=None):
        self.credentials: Dict = credentials or {}
        self.display_metadata: Dict = display_metadata or {}
        self.base_url = base_url

    async def aclose(self) -> None:  # pragma: no cover - optional
        return None

    @abc.abstractmethod
    async def healthcheck(self) -> dict: ...

    async def list_accounts(self) -> list:
        """All accounts on the login. Override per broker."""
        raise NotImplementedError

    @abc.abstractmethod
    async def get_account_info(self) -> AccountInfo: ...

    @abc.abstractmethod
    async def list_positions(self) -> List[BrokerPosition]: ...

    @abc.abstractmethod
    async def list_orders(self, status: Optional[OrderStatus] = None) -> List[BrokerOrder]: ...

    @abc.abstractmethod
    async def place_order(self, req: PlaceOrderRequest) -> BrokerOrder: ...

    @abc.abstractmethod
    async def cancel_order(self, broker_order_ref: str) -> bool: ...

    @abc.abstractmethod
    async def modify_position(self, req: ModifyPositionRequest) -> BrokerPosition: ...

    @abc.abstractmethod
    async def modify_order(self, req: ModifyOrderRequest) -> BrokerOrder: ...

    @abc.abstractmethod
    async def close_position(self, broker_position_ref: str,
                             quantity: Optional[Decimal] = None) -> ClosePositionResult: ...

    @abc.abstractmethod
    async def search_instrument(self, query: str) -> List[BrokerInstrument]: ...

    @abc.abstractmethod
    async def get_quote(self, broker_symbol: str) -> BrokerQuote: ...

    async def fetch_candles(self, broker_symbol: str, resolution: str,
                            frm: "dt.datetime", to: "dt.datetime", *,
                            max_bars: int = 1000) -> List[BrokerCandle]:
        """Historical bars for `broker_symbol` in [frm, to), oldest first.

        Deliberately NOT abstract: an adapter for a broker with no price-history
        endpoint is still a usable adapter, and forcing a stub on it would say
        otherwise. Callers that need bars should catch this and say which broker
        cannot supply them.

        It IS on the interface, though, because the alternative is what #257
        found: `scripts/candle_sync.py` re-implemented the whole Capital session
        stack - hostnames, CST/X-SECURITY-TOKEN, 401 re-auth, 429 back-off - to
        reach one endpoint the abstraction did not cover, putting a provider's
        hostnames outside the adapter layer.

        `resolution` is the provider's own vocabulary (Capital: MINUTE,
        MINUTE_5, HOUR ...). An empty list means the provider had no bars in the
        window, which is the normal answer over a weekend and NOT an error.
        """
        raise NotImplementedError(
            f"{type(self).__name__} has no price-history endpoint")
