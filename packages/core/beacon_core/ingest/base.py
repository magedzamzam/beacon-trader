"""Inbound-channel contract (#35). Mirrors the brokers/ side: a provider speaks
in these common types, and a registry builds concrete channels by kind."""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, Optional

from ..parsing.models import ParsedSignal


@dataclass
class InboundMessage:
    """One normalized inbound message. A concrete channel fills this in and hands
    it to the shared pipeline; it does NOT re-implement parse/validate/dedupe."""
    source_id: Optional[int]
    kind: str = "generic"                 # telegram | tradingview | api | manual ...
    text: str = ""                        # raw text (free-text path)
    parsed: Optional[ParsedSignal] = None # pre-parsed (structured path); else the
                                          # pipeline parses `text`
    from_freetext: bool = False           # True -> AI validates/corrects (parser
                                          # can misread levels); structured skips it
    is_live: bool = True                  # False -> historical backfill (never traded)
    # Channel message-log (written only when persist_message=True — Telegram keeps
    # a per-message history; webhooks/manual do not).
    persist_message: bool = False
    chat_key: Optional[str] = None
    message_id: Optional[int] = None
    sender: Optional[str] = None
    msg_date: Any = None
    reply_to: Optional[int] = None


@dataclass
class IngestResult:
    signal_id: Optional[int]
    status: str                # none | parsed | rejected | duplicate | seen
    accepted: bool             # validated AND not AI-rejected (publishable)
    reason: Optional[str]
    published: bool            # enqueued to the executor


class BaseInboundChannel(abc.ABC):
    """A signal source that owns its transport/event loop and feeds normalized
    InboundMessages into `ingest_message`."""
    kind: str = "generic"

    @abc.abstractmethod
    async def run(self) -> None:
        """Own the transport and stay resident, normalizing each raw provider
        message into an InboundMessage handed to the shared pipeline."""
        raise NotImplementedError
