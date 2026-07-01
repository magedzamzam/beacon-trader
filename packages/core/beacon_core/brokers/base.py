"""BrokerAdapter — the unified gateway contract. Add a broker by implementing
this interface; nothing above the adapter layer changes."""
from __future__ import annotations

import abc
from decimal import Decimal
from typing import Dict, List, Optional

from .types import (
    AccountInfo, BrokerInstrument, BrokerOrder, BrokerPosition, BrokerQuote,
    ClosePositionResult, ModifyOrderRequest, ModifyPositionRequest,
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
