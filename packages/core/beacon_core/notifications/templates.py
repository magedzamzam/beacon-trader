"""Customisable per-event notification templates (#139).

The message *body* is the only hardcoded piece of the notification pipeline; this
module makes it operator-editable from the frontend with dynamic `{token}`
substitution — no code, no deploy.

Pure: no DB / crypto / network — imports cleanly anywhere (like `config`).

A single field registry (`FIELDS`) is the one source of truth: `sample_ctx()` and
`field_descriptor()` are both derived from it, and the renderer resolves the same
token vocabulary. So the frontend field-picker can never advertise a token the
renderer can't resolve, and a new field lights up in the editor as soon as it's
added here.

Tokens are `{field}` — optionally dotted, e.g. `{ai.verdict}` — resolved by a
SAFE key-walk of the event's ctx dict. We never call `str.format` on raw ctx:
that would expose `{0.__class__}`-style attribute injection and raise on a
missing key. Unknown / missing tokens render empty; the renderer never raises.
"""
from __future__ import annotations

import re
from typing import Optional

# (token, human label, example value). Dotted tokens map to nested ctx dicts.
# Order here is the order the field chips render in the editor. Keep examples
# realistic — they drive the live preview.
FIELDS: list[tuple[str, str, str]] = [
    ("symbol",        "Symbol",           "XAUUSD"),
    ("direction",     "Direction",        "BUY"),
    ("channel",       "Channel / source", "Gold Signals VIP"),
    ("account",       "Account",          "Capital Demo"),
    ("size",          "Lot / position size", "0.10"),
    ("entry",         "Entry",            "3421.50"),
    ("price",         "Price",            "3428.10"),
    ("tp",            "Take-profit",      "3440, 3455"),
    ("sl",            "Stop-loss",        "3410.00"),
    ("pl",            "Net P&L",          "+42.10"),
    ("open_time",     "Open time (UTC)",  "2026-07-26 14:02"),
    ("close_time",    "Close time (UTC)", "2026-07-26 14:32"),
    ("ai.verdict",    "AI verdict",       "approve"),
    ("ai.confidence", "AI confidence",    "0.82"),
    ("detail",        "Detail",           "TP1 — tp_hit"),
]

# Per-event field CONTRACT — the tokens each event's emitter actually populates
# today. This is the honest, per-event field set: picking "Trade closed" must
# show only trade_closed's dict, not a global list. Signal-stage events read a
# Signal; execution/position events read a Trade. An event mapped to `[]` is in
# the routing matrix but NOT emitted by any service yet, so it carries nothing.
#
# SINGLE SOURCE OF TRUTH: this drives the field picker, the preview sample, AND
# the test-fire ctx (context.build_ctx) — so the editor, the test, and the live
# message can never disagree. To make a field available on an event that lacks it
# (e.g. {channel} on trade_closed), ENRICH that emitter, then add it here — don't
# advertise a token the emitter won't send. Every token must exist in FIELDS.
EVENT_FIELDS: dict[str, list[str]] = {
    "new_signal":       ["symbol", "direction", "entry", "sl", "tp", "channel"],
    "signal_validated": [],
    "signal_rejected":  [],
    "order_placed":     ["symbol", "direction", "size", "account", "channel", "detail"],
    "order_filled":     ["symbol", "direction", "size", "price", "detail"],
    "order_cancelled":  ["symbol", "direction", "detail"],
    "tp_hit":           ["symbol", "direction", "size", "price", "pl", "detail"],
    "sl_hit":           ["symbol", "direction", "size", "price", "pl", "detail"],
    "sl_moved":         ["symbol", "direction", "sl", "detail"],
    "trade_closed":     ["symbol", "direction", "size", "channel", "account", "pl",
                         "open_time", "close_time"],
    "broker_error":     ["symbol", "account", "detail"],
    "daily_summary":    [],
}


def is_emitted(event_id: str) -> bool:
    """True if some service actually fires this event today (non-empty contract).
    The routing matrix lists events that aren't wired up yet — the editor flags
    those so an operator doesn't write a template that never sends."""
    return bool(EVENT_FIELDS.get(event_id))


def _tokens_for(event_id: Optional[str]) -> list[str]:
    """The token list for an event, or every FIELDS token when event_id is None
    (back-compat full view)."""
    if event_id is None:
        return [name for name, _l, _e in FIELDS]
    return EVENT_FIELDS.get(event_id, [])


# `{token}` where token is letters/digits/underscore, dot-separated. Anything
# else (spaces, braces, punctuation) is left untouched as literal text.
_TOKEN = re.compile(r"\{([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)\}")


def _walk(ctx: dict, path: str):
    """Resolve a dotted token against ctx by pure dict-get. Returns None if any
    hop is missing or traverses a non-dict — never raises, never touches
    attributes (so `{x.__class__}` resolves to None, not the class object)."""
    cur = ctx
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def render(template: Optional[str], ctx: Optional[dict]) -> str:
    """Substitute `{token}` in `template` from ctx via the safe walk. A token
    that is missing/None/"" renders empty; the renderer never raises. Output is
    plain text — the channel senders do the HTML escaping (so a free-text
    template can't reintroduce the #76 unbalanced-HTML silent-drop)."""
    if not template:
        return ""
    ctx = ctx or {}

    def _sub(m: re.Match) -> str:
        v = _walk(ctx, m.group(1))
        return "" if v in (None, "") else str(v)

    return _TOKEN.sub(_sub, template)


def _set_path(d: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def sample_ctx(event_id: Optional[str] = None) -> dict:
    """A representative ctx the editor introspects and the live preview renders
    against. With an `event_id`, contains ONLY that event's contract fields (so
    the preview matches what the event can actually carry); without one, the full
    set (back-compat). Built from FIELDS so it stays in lockstep with the
    descriptor."""
    toks = set(_tokens_for(event_id))
    out: dict = {}
    for name, _label, example in FIELDS:
        if name in toks:
            _set_path(out, name, example)
    return out


def field_descriptor(event_id: Optional[str] = None) -> list[dict]:
    """Per-token descriptor powering the field chips: token, label, and the
    example value RESOLVED from the sample (so the advertised example is exactly
    what the renderer produces). With an `event_id`, only that event's contract
    fields — the honest per-event list."""
    toks = set(_tokens_for(event_id))
    sample = sample_ctx()                        # full sample for example lookup
    out = []
    for name, label, _example in FIELDS:
        if name in toks:
            v = _walk(sample, name)
            out.append({"token": name, "label": label,
                        "example": "" if v is None else str(v)})
    return out


def sanitize_templates(raw: Optional[dict], event_ids) -> dict:
    """Keep only known events; coerce subject/body to non-empty strings. An empty
    or missing subject/body means 'use the default for that part', so we simply
    drop it — clearing a field reverts to the byte-for-byte default message."""
    raw = raw or {}
    out: dict = {}
    for e in event_ids:
        t = raw.get(e)
        if not isinstance(t, dict):
            continue
        entry = {}
        for part in ("subject", "body"):
            v = t.get(part)
            if isinstance(v, str) and v != "":
                entry[part] = v
        if entry:
            out[e] = entry
    return out
