"""Broker-agnostic data contracts. Every adapter speaks in these types so the
rest of Beacon never imports a broker SDK directly."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, List, Optional


# ---- errors ---------------------------------------------------------------
class BrokerError(Exception):
    """Base for every broker failure."""


class AuthError(BrokerError):
    """Credentials rejected / session could not be established."""


class NetworkError(BrokerError):
    """Transport-level failure reaching the broker."""


class RateLimitError(BrokerError):
    """Broker throttled us (HTTP 429 or equivalent)."""


class NotFoundError(BrokerError):
    """Referenced position/order/instrument does not exist."""


# ---- enums ----------------------------------------------------------------
class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class OrderStatus(str, Enum):
    PENDING = "PENDING"      # submitted, awaiting broker confirm
    WORKING = "WORKING"      # resting limit/stop order
    FILLED = "FILLED"        # became an open position
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


def to_dec(v: Any) -> Optional[Decimal]:
    """Coerce broker JSON scalars to Decimal, tolerating None/str/float."""
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


# ---- value objects --------------------------------------------------------
@dataclass
class AccountInfo:
    account_id: str
    balance: Optional[Decimal] = None
    available: Optional[Decimal] = None
    currency: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BrokerQuote:
    broker_symbol: str
    bid: Optional[Decimal] = None
    offer: Optional[Decimal] = None
    last_price: Optional[Decimal] = None
    high_price: Optional[Decimal] = None
    low_price: Optional[Decimal] = None
    close_price: Optional[Decimal] = None
    change_abs: Optional[Decimal] = None
    change_pct: Optional[Decimal] = None
    currency: Optional[str] = None
    market_status: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BrokerInstrument:
    broker_symbol: str
    name: str
    instrument_type: Optional[str] = None
    currency: Optional[str] = None
    min_qty: Optional[Decimal] = None


@dataclass
class BrokerPosition:
    broker_symbol: str
    broker_position_ref: str
    quantity: Decimal
    avg_open_price: Optional[Decimal] = None
    current_price: Optional[Decimal] = None
    unrealized_pl: Optional[Decimal] = None
    unrealized_pl_pct: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    opened_at: Optional[Any] = None
    currency: Optional[str] = None
    direction: Direction = Direction.LONG
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BrokerOrder:
    broker_order_ref: str
    broker_symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    limit_price: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    status: OrderStatus = OrderStatus.PENDING
    fill_price: Optional[Decimal] = None
    fill_quantity: Optional[Decimal] = None
    currency: Optional[str] = None
    rejection_reason: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PlaceOrderRequest:
    broker_symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    limit_price: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None


@dataclass
class ModifyPositionRequest:
    broker_position_ref: str
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None


@dataclass
class ModifyOrderRequest:
    broker_order_ref: str
    limit_price: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None


@dataclass
class ClosePositionResult:
    broker_position_ref: str
    closed: bool
    closed_quantity: Optional[Decimal] = None
    close_price: Optional[Decimal] = None
    realized_pl: Optional[Decimal] = None
    raw: Dict[str, Any] = field(default_factory=dict)
