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


def sample_ctx() -> dict:
    """A fully-populated, representative ctx — the object the editor introspects
    and the live preview renders against. Built from FIELDS so it stays in
    lockstep with the field descriptor."""
    out: dict = {}
    for name, _label, example in FIELDS:
        _set_path(out, name, example)
    return out


def field_descriptor() -> list[dict]:
    """Per-token descriptor powering the @-picker and drag chips: token, label,
    and the example value RESOLVED from sample_ctx (so the advertised example is
    exactly what the renderer produces). One data source, two interaction
    modes."""
    sample = sample_ctx()
    out = []
    for name, label, _example in FIELDS:
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
