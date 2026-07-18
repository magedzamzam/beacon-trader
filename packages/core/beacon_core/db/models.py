"""Schema. Money is NUMERIC(18,6); Decimal end-to-end. Broker is the source of
truth for fills — these rows are Beacon's ledger, reconciled against it."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Index, Integer,
                        Numeric, String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from ..timeutil import utcnow as _now   # column default: tz-aware UTC now

NUM = Numeric(18, 6)


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
    # strategy: {entry_ttl_minutes, sl_rules:[...]}  (orders are LIMIT-with-market-fallback)
    strategy: Mapped[dict] = mapped_column(JSON, default=dict)
    risk_config: Mapped[dict] = mapped_column(JSON, default=dict)  # overrides account default
    account_map: Mapped[list] = mapped_column(JSON, default=list)  # [account_id, ...]
    archived: Mapped[bool] = mapped_column(Boolean, default=False)  # soft-delete: hide but keep attribution
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
    reinitiated_from: Mapped[int | None] = mapped_column(               # clone audit trail (#66)
        ForeignKey("signals.id"), nullable=True)
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
    # Execution-strategy attribution (#83/#84): the exit sl_rules this trade
    # actually ran under, SNAPSHOT at entry (point-in-time — immune to later
    # strategy edits, so an A/B arm's exit logic is frozen for valid attribution).
    # strategy_id names the ExecutionStrategy that produced it (NULL = fell back
    # to source/global default). Both NULL on pre-#83 trades -> monitor resolves live.
    strategy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sl_rules: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Correlation-cluster tag (#106): concurrent same-symbol/same-direction trades
    # share a cluster_id so aggregate exposure can be budgeted and P&L analysed
    # per-cluster as well as per-channel. cluster_alloc records what the cluster
    # budgeter computed/applied (shadow vs enforced). Both NULL until the feature
    # is enabled (needs the ALTER — new columns don't auto-appear, CLAUDE.md §6).
    cluster_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    cluster_alloc: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    signal: Mapped["Signal"] = relationship(back_populates="trades")
    legs: Mapped[list["Leg"]] = relationship(back_populates="trade")


class ExecutionStrategy(Base):
    """A per-(account, source) execution strategy (#84) — the single home for how
    a signal is entered, filtered, and exited on a given account.

    THREE PILLARS (each an independently-extensible JSON blob):
      * entry_policy   — order placement: entry TTL, chase-guard (#67), and future
                         entry types (delayed STOP, etc.). Source-type-agnostic.
      * entry_filters  — a rule set that can SKIP / de-size / up-size a trade from
                         Analytics / Bayesian / session / structure signals
                         (e.g. inside-FVG -> x2, NY overlap -> x0.5). Extensible.
      * exit_policy    — sl_rules ladder + cancel_pending_on_stop.

    SCOPE is (account_id, source_id), both NULLABLE, so defaults come for free:
    resolution picks the MOST-SPECIFIC enabled match —
        (acct, src) > (acct, *) > (*, src) > (*, *)  [see execution/strategy.py].
    A pillar left null falls back to the global/source default, so 'no strategy'
    is byte-identical to today. Risk lives on Risk & Limits, NOT here."""
    __tablename__ = "execution_strategies"
    __table_args__ = (UniqueConstraint("account_id", "source_id",
                                       name="uq_execution_strategy_scope"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True, index=True)   # NULL = any account
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id"), nullable=True, index=True)    # NULL = any source
    entry_policy: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    entry_filters: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    exit_policy: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)     # bumps on edit (attribution)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True),
                                                    default=_now, onupdate=_now)


class AccountSourceRisk(Base):
    """Per-(account, source) RISK override (#84). Risk lives on Risk & Limits, not
    in the execution strategy — this is the per-channel risk sizing for a specific
    account. Resolution: this override -> the account's own risk_config (the
    overall per-account risk) -> conservative default. `risk_config` is the same
    RiskConfig shape used on accounts (basis / value / allocation / per_tp_percent)."""
    __tablename__ = "account_source_risk"
    __table_args__ = (UniqueConstraint("account_id", "source_id",
                                       name="uq_account_source_risk"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    risk_config: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True),
                                                    default=_now, onupdate=_now)


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
    reply_to_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # links a follow-up to its signal
    message_date: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SignalClaim(Base):
    """A channel's claimed outcome for a signal, parsed from a follow-up message
    (e.g. 'TP2 HIT', 'SL HIT', 'all TP done'). Append-only; the reconciler
    compares these claims against what the bot actually did. One row per
    outcome message that resolved to a signal."""
    __tablename__ = "signal_claims"
    __table_args__ = (UniqueConstraint("message_id", name="uq_signal_claim_msg"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("telegram_messages.id"))
    max_tp_claimed: Mapped[int] = mapped_column(Integer, default=0)
    sl_claimed: Mapped[bool] = mapped_column(Boolean, default=False)
    all_tp: Mapped[bool] = mapped_column(Boolean, default=False)
    # How confidently this outcome message was linked to its signal (#63): 1.0 for
    # a direct Telegram reply, lower for a time-proximity match. NULL on pre-#63
    # rows -> treated as "unknown", never excluded for lack of data.
    claim_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PositionActivity(Base):
    """Broker-authoritative audit of everything that happened to a deal.

    One row per Capital.com /history/activity item (working order executed,
    position opened, SL/TP edited, position closed by SL/TP/user, …) plus the
    realized P&L + currency for closes (from /history/transactions). This is the
    'truth' record — the exact lifecycle and money of each working order and
    position — kept broker-agnostic and separate from the Leg so it can be mined
    for performance analysis later. Leg still carries the live dealId refs for
    reconciliation; this table never has to change if a broker's id scheme does.
    """
    __tablename__ = "position_activities"
    __table_args__ = (UniqueConstraint("account_id", "deal_id", "activity_at", "type",
                                       name="uq_activity_dedupe"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"), nullable=True, index=True)
    leg_id: Mapped[int | None] = mapped_column(ForeignKey("legs.id"), nullable=True, index=True)
    epic: Mapped[str | None] = mapped_column(String(32), nullable=True)
    deal_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    deal_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str | None] = mapped_column(String(24), nullable=True)   # SYSTEM|USER|SL|TP|...
    type: Mapped[str | None] = mapped_column(String(32), nullable=True)     # WORKING_ORDER|POSITION|EDIT_STOP_AND_LIMIT|...
    status: Mapped[str | None] = mapped_column(String(24), nullable=True)   # ACCEPTED|EXECUTED|...
    realized_pl: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)  # signed, account ccy (closes only)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    activity_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)               # raw broker activity
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


class SignalFeature(Base):
    """Technical-analysis snapshot captured at signal time — one row per signal.

    A multi-timeframe indicator context (RSI, MACD, EMA/SMA, ATR, swing
    support/resistance, Fibonacci) plus session/time, stored so trade outcomes
    can LATER be correlated with the TA conditions the signal fired under. SMC is
    intentionally deferred; add it to `features` when its rules are pinned down."""
    __tablename__ = "signal_features"
    __table_args__ = (UniqueConstraint("signal_id", name="uq_signal_feature"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"))
    symbol: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str | None] = mapped_column(String(4), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)          # reference price at capture
    session: Mapped[str | None] = mapped_column(String(8), nullable=True)      # ASIA|LONDON|OVERLAP|NY|LATE
    utc_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    features: Mapped[dict] = mapped_column(JSON, default=dict)                 # {timeframe: {indicator: value}}
    captured_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SignalAnalytics(Base):
    """Shadow analytics sidecar (#51/#52) — advanced quant estimators computed
    per signal, side-by-side with live trading and FULLY NON-BLOCKING. Pure
    observability: nothing here gates or alters execution. Joins to
    trades.realized_pl via signal_id (mirrors signal_features) for labelled
    correlation analysis. `window` keeps a compact price snapshot so estimators
    are reproducible offline; `degraded` lists estimators that failed this run."""
    __tablename__ = "signal_analytics"
    __table_args__ = (UniqueConstraint("signal_id", name="uq_signal_analytics"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"))
    symbol: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str | None] = mapped_column(String(4), nullable=True)
    regime: Mapped[str | None] = mapped_column(String(16), nullable=True)      # trending|ranging|high_vol
    price: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    window: Mapped[dict] = mapped_column(JSON, default=dict)                   # compact price window (reproducibility)
    analytics: Mapped[dict] = mapped_column(JSON, default=dict)                # {estimator: output}
    degraded: Mapped[list] = mapped_column(JSON, default=list)                 # estimators that errored this run
    captured_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MarketStructure(Base):
    """Persistent multi-TF market structure (#61), VERSIONED. Slow-moving: a
    weekly (config) / on-demand recompute writes a new `version_id` per symbol
    and supersedes the prior, so any signal can be joined to the map that was
    live when it fired (point-in-time). One row per (symbol, timeframe, version).
    Shadow-only — never gates execution."""
    __tablename__ = "market_structure"
    __table_args__ = (Index("ix_market_structure_active", "symbol", "active"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(4))
    version_id: Mapped[int] = mapped_column(Integer)            # groups a full recompute (per symbol)
    label: Mapped[str] = mapped_column(String(8))              # bull | bear | range
    swings: Mapped[list] = mapped_column(JSON, default=list)   # ordered pivots [{kind,price,idx}]
    bias_price: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)
    premium_discount: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)  # 0 discount -> 1 premium
    atr: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)               # TF ATR at compute (for dist_atr)
    last_event: Mapped[str | None] = mapped_column(String(8), nullable=True)      # BOS | CHoCH (future)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    superseded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StructureLevel(Base):
    """One row per individual level (#61) — the granularity that lets a future
    signal engine SELECT/weight/combine each level. Fib retracement/extension,
    swing highs/lows, (later) OB/FVG/equal-highs. Extensible `kind` enum."""
    __tablename__ = "structure_levels"
    __table_args__ = (Index("ix_structure_levels_active", "symbol", "active"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(4))
    version_id: Mapped[int] = mapped_column(Integer)
    structure_id: Mapped[int | None] = mapped_column(ForeignKey("market_structure.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))             # fib_retracement|fib_extension|swing_high|...
    ratio: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)   # e.g. 0.618 (null for swings)
    price: Mapped[Decimal] = mapped_column(NUM)
    anchor_a: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {price, idx} of the swings it's derived from
    anchor_b: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    anchor_c: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    direction: Mapped[str | None] = mapped_column(String(4), nullable=True)   # up | down (leg direction)
    weight: Mapped[Decimal] = mapped_column(NUM, default=0)   # tf_weight * kind_weight (config)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    superseded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MagnetZone(Base):
    """Cross-TF confluence clusters (#61) — the actual 'magnet' output. Σ(member
    weight) = confluence score; rank 1 = strongest. Versioned + point-in-time."""
    __tablename__ = "magnet_zones"
    __table_args__ = (Index("ix_magnet_zones_active", "symbol", "active"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    version_id: Mapped[int] = mapped_column(Integer)
    price_low: Mapped[Decimal] = mapped_column(NUM)
    price_high: Mapped[Decimal] = mapped_column(NUM)
    mid: Mapped[Decimal] = mapped_column(NUM)
    score: Mapped[Decimal] = mapped_column(NUM)
    rank: Mapped[int] = mapped_column(Integer)
    n_timeframes: Mapped[int] = mapped_column(Integer, default=0)
    ref_atr: Mapped[Decimal | None] = mapped_column(NUM, nullable=True)   # 1H ATR (for dist_atr)
    members: Mapped[list] = mapped_column(JSON, default=list)             # [{level_id,timeframe,kind,ratio,price}]
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    superseded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EconEvent(Base):
    """Economic-calendar events (GMT) for the Trading Hours news blackout.
    Fetched from a free calendar feed and persisted so they survive restarts."""
    __tablename__ = "econ_events"
    __table_args__ = (UniqueConstraint("ts", "ccy", "title", name="uq_econ_event"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)  # -> ix_econ_events_ts
    ccy: Mapped[str | None] = mapped_column(String(8), nullable=True)
    impact: Mapped[str | None] = mapped_column(String(16), nullable=True)      # high|medium|low|holiday
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
