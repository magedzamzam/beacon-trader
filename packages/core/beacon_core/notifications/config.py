"""Notification channels + event catalog, sanitizer, and secret-masked view.

Pure: no DB, no network, no crypto. Secret fields are stored as opaque
`<name>_enc` strings (encrypted by the API layer, like the AI config) and pass
through untouched here; `public_config` masks them to `has_<name>` booleans.
"""
from __future__ import annotations

from typing import Optional

from . import templates as _T

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
        # #200: an A/B arm that skips ~everything produces no trades and no
        # information. That is a broken experiment, not a conservative one, and
        # the last one ran dark for two days before a human noticed a flat
        # account — then fixed it in a way that orphaned the accumulation.
        {"id": "arm_dark", "label": "A/B arm not trading"},
        # #255: a categorical shadow estimator whose label takes ONE value across
        # a whole window carries no information — and the regime label has been in
        # that state in three separate weeks, each found by an analyst mid-report
        # rather than by anything watching.
        {"id": "estimator_degenerate", "label": "Estimator label degenerate"},
    ]},
]

# --- Severity + the delivery policy (#210) ------------------------------------
# Every routed event fired 24/7 at the same weight. On a busy XAUUSD day the
# routine stream buries the money/safety events, and there was no way to say
# "overnight, only the critical ones". `throttle.py` (#180) collapses ONE
# event's bursts; this is the cross-event, time-of-day axis it cannot see.
SEVERITY_RANK = {"info": 0, "summary": 1, "critical": 2}
SEVERITIES = ["info", "summary", "critical"]

EVENT_SEVERITY = {
    # money and safety — these must survive any gate an operator sets
    "tp_hit": "critical", "sl_hit": "critical", "trade_closed": "critical",
    "broker_error": "critical", "arm_dark": "critical",
    # the end-of-day roll-up: more than chatter, less than a stop-out
    "daily_summary": "summary",
    # measurement integrity, not money: it should reach the operator before the
    # next weekly, not wake them at 03:00 (#255).
    "estimator_degenerate": "summary",
    # routine flow
    "new_signal": "info", "signal_validated": "info", "signal_rejected": "info",
    "order_placed": "info", "order_filled": "info", "order_cancelled": "info",
    "sl_moved": "info",
}

QUIET_HOURS_DEFAULT = {"enabled": False, "start_utc": "22:00",
                       "end_utc": "06:00", "min_severity": "critical"}


def event_severity(event_id: str) -> str:
    """An unmapped event is `critical`: a new event type must not become
    silenceable by having been forgotten here."""
    return EVENT_SEVERITY.get(event_id, "critical")


def _hhmm(v):
    """"HH:MM" -> minutes since midnight, else None."""
    if v is None:
        return None
    parts = str(v).strip().split(":")
    if len(parts) < 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return None
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    return h * 60 + m


def in_quiet_window(policy, now) -> bool:
    """Is `now` (an aware/UTC datetime) inside the configured window?

    Half-open `[start, end)`; `end <= start` crosses midnight, which is the only
    reading that makes an overnight window expressible. An unreadable bound is
    NOT quiet — a malformed policy must never be able to silence an alert."""
    lo, hi = _hhmm((policy or {}).get("start_utc")), _hhmm((policy or {}).get("end_utc"))
    if lo is None or hi is None or lo == hi:
        return False
    minute = now.hour * 60 + now.minute
    return lo <= minute < hi if lo < hi else (minute >= lo or minute < hi)


def is_quiet_suppressed(event_id: str, policy, now) -> bool:
    """Should `event_id` be HELD (not dropped) right now?

    Fail-loud by construction: disabled policy, unreadable window, unknown
    min_severity and unmapped event all resolve to "deliver". The only way to
    stop a notification is a policy that explicitly says so."""
    policy = policy or {}
    if not policy.get("enabled"):
        return False
    floor = SEVERITY_RANK.get(str(policy.get("min_severity") or "").strip().lower())
    if floor is None:
        return False
    if not in_quiet_window(policy, now):
        return False
    return SEVERITY_RANK[event_severity(event_id)] < floor


# --- Policy validation (shared by the API write path, #209/#210) --------------
POLICY_KEYS = ("daily_summary", "arm_dark", "quiet_hours")


def _num(v, default, lo, hi, *, cast=float):
    try:
        n = cast(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def clean_policy(key: str, value) -> dict:
    """Whitelist + clamp one policy block. The monitor and dispatch already read
    these defensively, but the API should not PERSIST junk they then have to
    tolerate — a stored `at_utc: "banana"` is a digest that silently never
    arrives, and the operator has a settings screen telling them it is on."""
    v = value if isinstance(value, dict) else {}
    out = {"enabled": bool(v.get("enabled"))}
    if key == "daily_summary":
        at = v.get("at_utc")
        out["at_utc"] = at if _hhmm(at) is not None else "00:15"
    elif key == "arm_dark":
        out["window_hours"] = _num(v.get("window_hours", 24), 24, 1, 168)
        out["cooldown_hours"] = _num(v.get("cooldown_hours", 6), 6, 0.25, 168)
        out["min_signals"] = _num(v.get("min_signals", 10), 10, 1, 1000, cast=int)
        out["threshold"] = _num(v.get("threshold", 0.80), 0.80, 0.0, 1.0)
    elif key == "quiet_hours":
        for f, dflt in (("start_utc", "22:00"), ("end_utc", "06:00")):
            got = v.get(f, dflt)
            out[f] = got if _hhmm(got) is not None else dflt
        sev = str(v.get("min_severity") or "").strip().lower()
        out["min_severity"] = sev if sev in SEVERITY_RANK else "critical"
    else:
        raise ValueError(f"unknown policy key '{key}'")
    return out


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
    "templates": {},
}


def catalog() -> dict:
    """Everything the UI needs to render the channels + routing matrix + the
    per-event template editor. `fields_by_event`/`samples` are the honest
    per-event field lists + preview objects (only what each event carries);
    `emitted` flags events that aren't fired by any service yet. `fields`/`sample`
    are the full set, kept for back-compat."""
    return {
        "channels": CHANNELS, "event_groups": EVENT_GROUPS,
        "fields": _T.field_descriptor(), "sample": _T.sample_ctx(),
        "fields_by_event": {e: _T.field_descriptor(e) for e in EVENT_IDS},
        "samples": {e: _T.sample_ctx(e) for e in EVENT_IDS},
        "emitted": {e: _T.is_emitted(e) for e in EVENT_IDS},
        # #210: the operator setting a min-severity has to be able to see what
        # that will and will not deliver, beside the control that sets it.
        "severity": {e: event_severity(e) for e in EVENT_IDS},
        "severities": SEVERITIES,
    }


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

    out_templates = _T.sanitize_templates(cfg.get("templates"), EVENT_IDS)
    return {"channels": out_channels, "routing": out_routing,
            "templates": out_templates}


def public_config(cfg: Optional[dict]) -> dict:
    """UI-safe view: secret `<name>_enc` values become `has_<name>` booleans."""
    c = sanitize_config(cfg)
    for ch in CHANNELS:
        d = c["channels"][ch["id"]]
        for f in ch["fields"]:
            if f["secret"]:
                d[f"has_{f['name']}"] = bool(d.pop(f["name"] + "_enc", None))
    return c
