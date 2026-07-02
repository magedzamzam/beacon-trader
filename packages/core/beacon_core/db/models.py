"""Schema. Money is NUMERIC(18,6); Decimal end-to-end. Broker is the source of
truth for fills — these rows are Beacon's ledger, reconciled against it."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Integer, Numeric,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

NUM = Numeric(18, 6)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Broker(Base):
    __tablename__ = "brokers"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(32))            # "capital.com"
    name: Mapped[str] = mapped_column(String(64))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Phase 1: credentials via .env-referenced keys, not raw secrets in DB.
    credentials_ref: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    accounts: Mapped[list["Account"]] = relationship(back_populates="broker")


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    broker_id: Mapped[int] = mapped_column(ForeignKey("brokers.id"))
    broker_account_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_config: Mapped[dict] = mapped_column(JSON, default=dict)   # RiskConfig dict
    broker: Mapped["Broker"] = relationship(back_populates="accounts")


class SymbolMap(Base):
    """internal symbol (XAUUSD) -> broker epic (GOLD) + sizing constants."""
    __tablename__ = "symbol_maps"
    __table_args__ = (UniqueConstraint("broker_id", "internal_symbol"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    broker_id: Mapped[int] = mapped_column(ForeignKey("brokers.id"))
    internal_symbol: Mapped[str] = mapped_column(String(16))
    broker_epic: Mapped[str] = mapped_column(String(32))
    value_per_point: Mapped[Decimal] = mapped_column(NUM, default=Decimal("1"))
    min_lot: Mapped[Decimal] = mapped_column(NUM, default=Decimal("0.01"))
    lot_step: Mapped[Decimal] = mapped_column(NUM, default=Decimal("0.01"))
    min_stop_distance: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)


class Source(Base):
    """A signal origin: a telegram channel, a tradingview webhook, manual, api."""
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))            # telegram|tradingview|manual|api
    name: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # channel_id / api key
    enabled_for_trading: Mapped[bool] = mapped_column(Boolean, default=False)
    is_trusted: Mapped[bool] = mapped_column(Boolean, default=False)
    # strategy: {order_position_type, entry_ttl_minutes, sl_rules:[...]}
    strategy: Mapped[dict] = mapped_column(JSON, default=dict)
    risk_config: Mapped[dict] = mapped_column(JSON, default=dict)  # overrides account default
    account_map: Mapped[list] = mapped_column(JSON, default=list)  # [account_id, ...]
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    symbol: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str] = mapped_column(String(4))
    entry_from: Mapped[Decimal] = mapped_column(NUM)
    entry_to: Mapped[Decimal] = mapped_column(NUM)
    sl: Mapped[Decimal] = mapped_column(NUM)
    tps: Mapped[list] = mapped_column(JSON, default=list)
    order_type: Mapped[str] = mapped_column(String(8), default="MARKET")
    status: Mapped[str] = mapped_column(String(16), default="received")  # received|validated|rejected|executed
    reject_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    market_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)   # price/spread at signal time
    dedupe_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    trades: Mapped[list["Trade"]] = relationship(back_populates="signal")


class Trade(Base):
    """One signal executed on one account -> a group of legs (the fanout)."""
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    symbol: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str] = mapped_column(String(4))
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|closed|partial
    planned_risk: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    realized_pl: Mapped[Decimal] = mapped_column(NUM, default=Decimal("0"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    signal: Mapped["Signal"] = relationship(back_populates="trades")
    legs: Mapped[list["Leg"]] = relationship(back_populates="trade")


class Leg(Base):
    """One broker order/position: a single (entry, tp) with the shared SL."""
    __tablename__ = "legs"
    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"))
    tp_index: Mapped[int] = mapped_column(Integer)
    order_type: Mapped[str] = mapped_column(String(8))
    entry: Mapped[Decimal] = mapped_column(NUM)
    tp: Mapped[Decimal] = mapped_column(NUM)
    sl: Mapped[Decimal] = mapped_column(NUM)
    lot: Mapped[Decimal] = mapped_column(NUM)
    # broker linkage
    broker_order_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)   # working order dealId
    broker_position_ref: Mapped[str | None] = mapped_column(String(64), nullable=True) # open position dealId
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # pending|working|open|closed|rejected|cancelled|expired
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)  # tp_hit|sl_hit|manual|expired
    fill_price: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    close_price: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    realized_pl: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    sl_moved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trade: Mapped["Trade"] = relationship(back_populates="legs")


class Event(Base):
    """Append-only audit/history: every decision and broker interaction."""
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"), nullable=True)
    leg_id: Mapped[int | None] = mapped_column(ForeignKey("legs.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class User(Base):
    """Portal login. Lets you sign in with username/password from any browser
    instead of pasting the API token each time (see beacon_core.security)."""
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Setting(Base):
    """Runtime, editable-from-the-UI configuration (JSON value per key).

    Keeps the platform 'fully configurable' without a redeploy: AI provider
    config, feature toggles, and general options live here rather than in .env.
    Secrets stored here are Fernet-encrypted (see beacon_core.crypto)."""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)


class TelegramMessage(Base):
    """Every message seen on a watched channel — signal or not — kept as an
    auditable, searchable history. `is_signal`/`signal_id` link the ones that
    parsed into a Signal so you can see, per channel, what became a trade."""
    __tablename__ = "telegram_messages"
    __table_args__ = (UniqueConstraint("chat_id", "message_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    chat_id: Mapped[str] = mapped_column(String(64), index=True)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sender: Mapped[str | None] = mapped_column(String(128), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_signal: Mapped[bool] = mapped_column(Boolean, default=False)
    parse_status: Mapped[str] = mapped_column(String(16), default="none")  # none|parsed|rejected
    reject_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    message_date: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AiAssessment(Base):
    """An AI judgement about a signal, a planned execution, or a closed trade.

    kind: signal_validation | execution_review | outcome_analysis
    verdict: approve | caution | reject | (analysis for outcomes)
    Kept append-only so you can audit what the model said and when."""
    __tablename__ = "ai_assessments"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True, index=True)
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"), nullable=True, index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    provider: Mapped[str] = mapped_column(String(24), default="anthropic")
    model: Mapped[str | None] = mapped_column(String(48), nullable=True)
    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)   # 0..1
    score: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)        # 0..100 quality
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)                # full structured result
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
