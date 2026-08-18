"""Notification service — channels, per-event routing, and delivery.

`config` (catalog/sanitize/public_config) is pure — no crypto/DB/network deps —
so it imports cleanly anywhere. The delivery surface (`notify`, `send_test`,
`format_message`) lives in `dispatch`, which pulls in crypto + settings; it's
exposed lazily so importing the pure config side never requires the full stack.
"""
from .config import (
    CHANNELS, EVENT_GROUPS, EVENT_IDS, CHANNEL_IDS, DEFAULT_CONFIG,
    POLICY_KEYS, SETTING_KEY, catalog, clean_policy, sanitize_config,
    public_config,
)
from .templates import field_descriptor, sample_ctx
from .throttle import Throttle

__all__ = [
    "CHANNELS", "EVENT_GROUPS", "EVENT_IDS", "CHANNEL_IDS", "DEFAULT_CONFIG",
    "POLICY_KEYS", "SETTING_KEY", "catalog", "clean_policy",
    "sanitize_config", "public_config",
    "field_descriptor", "sample_ctx", "Throttle",
    "notify", "send_test", "format_message", "render_event", "send_event_to_channel",
]

_LAZY = {"notify", "send_test", "format_message", "render_event",
         "send_event_to_channel"}


def __getattr__(name):
    if name in _LAZY:
        from . import dispatch
        return getattr(dispatch, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
