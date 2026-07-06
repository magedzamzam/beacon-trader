"""Notification service — channels, per-event routing, and (later) delivery.

This first slice is configuration only: a catalog of channels and event types,
a sanitizer, and a UI-safe (secret-masked) view. Actual dispatch is a later
phase — nothing here sends anything yet.
"""
from .config import (
    CHANNELS, EVENT_GROUPS, EVENT_IDS, CHANNEL_IDS, DEFAULT_CONFIG,
    SETTING_KEY, catalog, sanitize_config, public_config,
)

__all__ = [
    "CHANNELS", "EVENT_GROUPS", "EVENT_IDS", "CHANNEL_IDS", "DEFAULT_CONFIG",
    "SETTING_KEY", "catalog", "sanitize_config", "public_config",
]
