from typing import List, Optional
from pydantic import BaseModel


class BrokerIn(BaseModel):
    type: str = "capital.com"
    name: str
    is_demo: bool = True
    enabled: bool = True
    # Either reference env vars (credentials_ref with *_env keys) OR provide the
    # secrets directly and they are stored encrypted in the DB (*_enc keys).
    credentials_ref: dict = {}
    api_key: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None


class AiConfigIn(BaseModel):
    enabled: bool = False
    provider: str = "anthropic"
    model: str = "claude-opus-4-8"
    validate_signals: bool = True
    review_execution: bool = True
    validation_mode: str = "block"      # off | block | background
    review_mode: str = "block"          # off | block | background
    analyze_outcomes: bool = True
    gate_execution: bool = False
    min_confidence: float = 0.0
    # Fast hot-path validation/correction of free-text signals.
    validation_model: str = "claude-haiku-4-5-20251001"
    validation_timeout_seconds: float = 5.0
    validation_thinking: bool = False
    api_key: Optional[str] = None          # write-only; stored encrypted


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
