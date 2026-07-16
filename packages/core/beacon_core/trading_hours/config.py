"""Trading Hours configuration defaults + sanitizer (pure — no DB/network)."""
from __future__ import annotations

from typing import Optional

from . import sessions

# Tier-2 window + keyword list for CPI/NFP/FOMC-grade releases (#77). A ±3-min
# window is far too tight for these — 07-14 CPI detonated ~-21k on entries 4-9
# min before the print. Widen to -30/+15 for named majors; keep ±3 for other
# high-impact. gate_entries wires this to the executor's entry path.
DEFAULT_MAJOR_KEYWORDS = [
    "cpi", "core cpi", "consumer price", "non-farm", "nonfarm", "nfp",
    "fomc", "fed interest rate", "federal funds", "interest rate decision",
    "ppi", "producer price", "powell", "fed chair", "testimony", "rate statement",
]

DEFAULT_CONFIG = {
    "sessions": sessions.DEFAULT_SESSIONS,
    "news": {"enabled": True, "gate_entries": True, "impacts": ["high"],
             "before_min": 3, "after_min": 3, "currencies": [],
             "major_before_min": 30, "major_after_min": 15,
             "major_keywords": DEFAULT_MAJOR_KEYWORDS},
    "holidays": {"enabled": True, "block_weekend": True, "block_us_holidays": True},
}


def sanitize_config(cfg: Optional[dict]) -> dict:
    cfg = cfg or {}
    out = {"sessions": [], "news": {}, "holidays": {}}
    for s in (cfg.get("sessions") or DEFAULT_CONFIG["sessions"]):
        if not all(k in s for k in ("id", "tz", "start", "end")):
            continue
        try:                                     # risk_mult (#81): de-size only, 0..1
            rm = max(0.0, min(1.0, float(s.get("risk_mult", 1.0))))
        except (TypeError, ValueError):
            rm = 1.0
        out["sessions"].append({
            "id": str(s["id"]), "label": s.get("label", s["id"]),
            "tz": str(s["tz"]), "start": str(s["start"]), "end": str(s["end"]),
            "enabled": bool(s.get("enabled", True)), "risk_mult": rm})
    if not out["sessions"]:
        out["sessions"] = list(sessions.DEFAULT_SESSIONS)

    n = cfg.get("news") or {}
    dn = DEFAULT_CONFIG["news"]
    # major_* fall back to the standard window when unset so a stored pre-#77 row
    # still parses; a stored major window narrower than the standard is lifted to
    # at least the standard (a "major" event can never black out for less time).
    _before = max(0, int(n.get("before_min", dn["before_min"]) or 0))
    _after = max(0, int(n.get("after_min", dn["after_min"]) or 0))
    out["news"] = {
        "enabled": bool(n.get("enabled", dn["enabled"])),
        "gate_entries": bool(n.get("gate_entries", dn["gate_entries"])),
        "impacts": [str(i).lower() for i in (n.get("impacts") or dn["impacts"])],
        "before_min": _before,
        "after_min": _after,
        "currencies": [str(c).upper() for c in (n.get("currencies") or [])],
        "major_before_min": max(_before, int(n.get("major_before_min", dn["major_before_min"]) or 0)),
        "major_after_min": max(_after, int(n.get("major_after_min", dn["major_after_min"]) or 0)),
        "major_keywords": [str(k).lower() for k in
                           (n.get("major_keywords") or dn["major_keywords"])],
    }
    h = cfg.get("holidays") or {}
    out["holidays"] = {
        "enabled": bool(h.get("enabled", True)),
        "block_weekend": bool(h.get("block_weekend", True)),
        "block_us_holidays": bool(h.get("block_us_holidays", True)),
    }
    return out
