"""Inbound-channel registry (#35) — mirrors brokers/registry so a host process
(e.g. the collector) can build N channels by kind from the Source.kind column.

Concrete channels live in their service (they own a transport dependency —
Telethon, an HTTP server, …) and register themselves at import time, keeping
this core module dependency-free."""
from __future__ import annotations

from typing import Callable, Dict

from .base import BaseInboundChannel

_CHANNELS: Dict[str, Callable[..., BaseInboundChannel]] = {}


def register_channel(kind: str, factory: Callable[..., BaseInboundChannel]) -> None:
    """Register a channel factory under a `Source.kind` (idempotent)."""
    _CHANNELS[kind] = factory


def get_channel(kind: str, config=None) -> BaseInboundChannel:
    """Build the channel registered for `kind`. Raises KeyError if none."""
    try:
        factory = _CHANNELS[kind]
    except KeyError:
        raise KeyError(f"no inbound channel registered for kind '{kind}'")
    return factory(config) if config is not None else factory()
