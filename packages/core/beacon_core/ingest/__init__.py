"""Provider-agnostic inbound-signal ingestion (#35).

One pipeline — parse -> validate -> dedupe -> persist -> publish — behind a
common `InboundMessage` contract, so every listener (Telegram, TradingView/
webhook, manual, or a future Discord/MT4 bridge) shares the SAME dedupe key
and Signal construction instead of reimplementing them divergently.
"""
from .base import BaseInboundChannel, InboundMessage, IngestResult
from .pipeline import ingest_message
from .registry import get_channel, register_channel

__all__ = ["BaseInboundChannel", "InboundMessage", "IngestResult",
           "ingest_message", "get_channel", "register_channel"]
