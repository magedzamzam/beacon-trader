"""Trading Hours configuration defaults + sanitizer (pure — no DB/network)."""
from __future__ import annotations

from typing import Optional

from . import sessions

DEFAULT_CONFIG = {
    "sessions": sessions.DEFAULT_SESSIONS,
    "news": {"enabled": True, "impacts": ["high"], "before_min": 3, "after_min": 3,
             "currencies": []},
    "holidays": {"enabled": True, "block_weekend": True, "block_us_holidays": True},
}


def sanitize_config(cfg: Optional[dict]) -> dict:
    cfg = cfg or {}
    out = {"sessions": [], "news": {}, "holidays": {}}
    for s in (cfg.get("sessions") or DEFAULT_CONFIG["sessions"]):
        if not all(k in s for k in ("id", "tz", "start", "end")):
            continue
        out["sessions"].append({
            "id": str(s["id"]), "label": s.get("label", s["id"]),
            "tz": str(s["tz"]), "start": str(s["start"]), "end": str(s["end"]),
            "enabled": bool(s.get("enabled", True))})
    if not out["sessions"]:
        out["sessions"] = list(sessions.DEFAULT_SESSIONS)

    n = cfg.get("news") or {}
    dn = DEFAULT_CONFIG["news"]
    out["news"] = {
        "enabled": bool(n.get("enabled", dn["enabled"])),
        "impacts": [str(i).lower() for i in (n.get("impacts") or dn["impacts"])],
        "before_min": max(0, int(n.get("before_min", dn["before_min"]) or 0)),
        "after_min": max(0, int(n.get("after_min", dn["after_min"]) or 0)),
        "currencies": [str(c).upper() for c in (n.get("currencies") or [])],
    }
    h = cfg.get("holidays") or {}
    out["holidays"] = {
        "enabled": bool(h.get("enabled", True)),
        "block_weekend": bool(h.get("block_weekend", True)),
        "block_us_holidays": bool(h.get("block_us_holidays", True)),
    }
    return out
