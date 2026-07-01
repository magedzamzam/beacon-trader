from typing import List, Optional
from pydantic import BaseModel


class BrokerIn(BaseModel):
    type: str = "capital.com"
    name: str
    is_demo: bool = True
    enabled: bool = True
    credentials_ref: dict = {}


class AccountIn(BaseModel):
    broker_id: int
    broker_account_id: str
    name: str
    currency: str = "USD"
    enabled: bool = False
    risk_config: dict = {}


class SymbolMapIn(BaseModel):
    broker_id: int
    internal_symbol: str
    broker_epic: str
    value_per_point: float = 1.0
    min_lot: float = 0.01
    lot_step: float = 0.01
    min_stop_distance: Optional[float] = None


class SourceIn(BaseModel):
    kind: str                       # telegram|tradingview|manual|api
    name: str
    external_id: Optional[str] = None
    enabled_for_trading: bool = False
    is_trusted: bool = False
    strategy: dict = {}
    risk_config: dict = {}
    account_map: List[int] = []


class ManualSignalIn(BaseModel):
    source_id: int
    symbol: str
    direction: str                  # BUY|SELL
    entry_from: float
    entry_to: float
    sl: float
    tps: List[float]
    order_type: str = "MARKET"      # MARKET|LIMIT
