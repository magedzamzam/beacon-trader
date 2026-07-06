"""Notification channels + event catalog, sanitizer, and secret-masked view.

Pure: no DB, no network, no crypto. Secret fields are stored as opaque
`<name>_enc` strings (encrypted by the API layer, like the AI config) and pass
through untouched here; `public_config` masks them to `has_<name>` booleans.
"""
from __future__ import annotations

from typing import Optional

SETTING_KEY = "notifications"


def _f(name, label, type="text", *, secret=False, default=None,
       options=None, placeholder=None):
    return {"name": name, "label": label, "type": type, "secret": secret,
            "default": default, "options": options, "placeholder": placeholder}


# --- Channels ------------------------------------------------------------------
# Each channel: id, label, a one-word hint, and the fields that configure it.
# `type` ∈ text | password | number | bool | select. `secret: True` fields are
# encrypted at rest and never returned to the UI.
CHANNELS = [
    {"id": "email", "label": "Email", "hint": "SMTP", "fields": [
        _f("from_addr", "From address", placeholder="bot@yourdomain.com"),
        _f("to_addrs", "To (comma-separated)", placeholder="me@x.com, ops@x.com"),
        _f("smtp_host", "SMTP host", placeholder="smtp.gmail.com"),
        _f("smtp_port", "SMTP port", "number", default=587),
        _f("smtp_user", "SMTP username"),
        _f("smtp_password", "SMTP password", secret=True),
        _f("use_tls", "Use TLS", "bool", default=True),
    ]},
    {"id": "telegram", "label": "Telegram", "hint": "Bot API", "fields": [
        _f("bot_token", "Bot token", secret=True, placeholder="123456:ABC-DEF…"),
        _f("chat_id", "Chat ID", placeholder="-1001234567890"),
    ]},
    {"id": "whatsapp", "label": "WhatsApp", "hint": "Twilio", "fields": [
        _f("account_sid", "Account SID"),
        _f("auth_token", "Auth token", secret=True),
        _f("from_number", "From", placeholder="whatsapp:+14155238886"),
        _f("to_number", "To", placeholder="whatsapp:+201234567890"),
    ]},
    {"id": "sms", "label": "SMS", "hint": "Twilio", "fields": [
        _f("account_sid", "Account SID"),
        _f("auth_token", "Auth token", secret=True),
        _f("from_number", "From", placeholder="+14155238886"),
        _f("to_number", "To", placeholder="+201234567890"),
    ]},
    {"id": "webhook", "label": "Webhook", "hint": "HTTP", "fields": [
        _f("url", "URL", placeholder="https://…"),
        _f("method", "Method", "select", default="POST", options=["POST", "PUT"]),
        _f("secret", "Signing secret (HMAC)", secret=True),
        _f("header_name", "Auth header name", placeholder="X-Api-Key (optional)"),
        _f("header_value", "Auth header value", secret=True),
    ]},
    {"id": "push", "label": "Push Notifications", "hint": "API", "fields": [
        _f("url", "Push endpoint URL", placeholder="https://…"),
        _f("api_key", "API key", secret=True),
    ]},
]

# --- Event types (rows of the routing matrix) ---------------------------------
EVENT_GROUPS = [
    {"group": "Signals", "events": [
        {"id": "new_signal", "label": "New signal"},
        {"id": "signal_validated", "label": "Signal validated (AI)"},
        {"id": "signal_rejected", "label": "Signal rejected (AI)"},
    ]},
    {"group": "Execution", "events": [
        {"id": "order_placed", "label": "Order placed"},
        {"id": "order_filled", "label": "Order filled / entry"},
        {"id": "order_cancelled", "label": "Order cancelled"},
    ]},
    {"group": "Position management", "events": [
        {"id": "tp_hit", "label": "Take-profit hit"},
        {"id": "sl_hit", "label": "Stop-loss hit"},
        {"id": "sl_moved", "label": "Stop moved (BE / trail)"},
        {"id": "trade_closed", "label": "Trade closed"},
    ]},
    {"group": "System", "events": [
        {"id": "broker_error", "label": "Broker / connection error"},
        {"id": "daily_summary", "label": "Daily summary"},
    ]},
]

CHANNEL_IDS = [c["id"] for c in CHANNELS]
EVENT_IDS = [e["id"] for g in EVENT_GROUPS for e in g["events"]]
_CHANNEL_BY_ID = {c["id"]: c for c in CHANNELS}


def _coerce(v, f):
    t = f["type"]
    if t == "bool":
        return bool(v)
    if t == "number":
        try:
            n = float(v)
            return int(n) if n == int(n) else n
        except (TypeError, ValueError):
            return f.get("default")
    if t == "select":
        return v if v in (f.get("options") or []) else f.get("default")
    return "" if v is None else str(v)


def _default_channel(ch):
    d = {"enabled": False}
    for f in ch["fields"]:
        if not f["secret"]:
            d[f["name"]] = f.get("default") if f.get("default") is not None else \
                (False if f["type"] == "bool" else "")
    return d


DEFAULT_CONFIG = {
    "channels": {c["id"]: _default_channel(c) for c in CHANNELS},
    "routing": {e: [] for e in EVENT_IDS},
}


def catalog() -> dict:
    """Everything the UI needs to render the channels + routing matrix."""
    return {"channels": CHANNELS, "event_groups": EVENT_GROUPS}


def sanitize_config(cfg: Optional[dict]) -> dict:
    """Coerce field types, drop unknown channels/fields/events. Opaque `_enc`
    secret values pass through untouched."""
    cfg = cfg or {}
    in_channels = cfg.get("channels") or {}
    out_channels = {}
    for ch in CHANNELS:
        src = in_channels.get(ch["id"]) or {}
        dst = {"enabled": bool(src.get("enabled", False))}
        for f in ch["fields"]:
            name = f["name"]
            if f["secret"]:
                enc = src.get(name + "_enc")
                if enc:
                    dst[name + "_enc"] = str(enc)
            else:
                dst[name] = _coerce(src.get(name, f.get("default")), f)
        out_channels[ch["id"]] = dst

    in_routing = cfg.get("routing") or {}
    out_routing = {}
    for e in EVENT_IDS:
        sel = in_routing.get(e) or []
        seen, keep = set(), []
        for c in sel:
            if c in _CHANNEL_BY_ID and c not in seen:
                seen.add(c)
                keep.append(c)
        out_routing[e] = keep
    return {"channels": out_channels, "routing": out_routing}


def public_config(cfg: Optional[dict]) -> dict:
    """UI-safe view: secret `<name>_enc` values become `has_<name>` booleans."""
    c = sanitize_config(cfg)
    for ch in CHANNELS:
        d = c["channels"][ch["id"]]
        for f in ch["fields"]:
            if f["secret"]:
                d[f"has_{f['name']}"] = bool(d.pop(f["name"] + "_enc", None))
    return c
