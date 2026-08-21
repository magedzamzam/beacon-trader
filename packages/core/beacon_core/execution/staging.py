"""Staged entry — the config and the tranche lifecycle (#129, cut down by #250).

The ENGINE that used to live here is gone. It partitioned a TP ladder into
toe-in / runner / reclaim roles, armed a re-entry STOP on a break-then-reclaim
beyond the zone's deep edge, and drove all of it from a pipeline of modifier
deciders over thirteen tuning numbers. `execution/ladder.py` replaced it: a table
of IF/THEN rows the operator can read, where each row is one order and the only
inputs are the signal's own entry levels and TP count.

Why it went rather than being tuned: `staged_filled` was 4 of 61 (#140) — the
design deferred most of the position behind conditions that rarely triggered —
and not one of its thirteen settings had ever been changed from its default by
anyone, on any account, in any replay variant. A model nobody could steer, and
nobody had steered, is not a model with promising defaults.

What stays is what the ladder still needs and did not replace:

  * the `entry_style` enum, and validation of the staged block;
  * the tranche STATE vocabulary, which the ladder reuses wholesale — a rung
    still goes pending -> deployed/armed -> filled/expired/cancelled, and the
    monitor's reconciliation is written against those names;
  * the two #158 order-age brakes and the expiry precedence they drive. A rung
    deploys late by design and its legs start a fresh TTL clock at that moment,
    so a staged order can outlive the control account's by the whole pending
    wait. These bound that window, and are OFF by default.

PURE — stdlib only.
"""
from __future__ import annotations

from typing import Optional

# The entry-order styles a strategy may choose. "staged" turns the ladder on;
# "market"/"limit" are the existing single-shot behaviours (the planner default).
ENTRY_STYLES = ("market", "limit", "staged")

# --- tranche states (persisted per tranche on the sidecar row) ----------------
PENDING = "pending"      # not yet deployed — waiting for its trigger level
DEPLOYED = "deployed"    # a LIMIT/MARKET order is working/open for this tranche
ARMED = "armed"          # a STOP is resting at the broker
FILLED = "filled"        # at least one leg filled
EXPIRED = "expired"      # gave up waiting
SKIPPED = "skipped"      # never deployed, deliberately
CANCELLED = "cancelled"  # its resting order was pulled at the broker (#161)

# A tranche in any of these is RESOLVED: it owns nothing at the broker any more,
# so "armed with a live broker_order_ref" is never a permanent state (#161).
TERMINAL_STATES = (FILLED, EXPIRED, SKIPPED, CANCELLED)


# The whole staged config: a master flag, and the two #158 brakes.
#
# Thirteen tuning numbers used to sit here (the partition tiers, the
# break-then-reclaim geometry, the per-role TTLs). #250 deleted them along with
# the engine that read them. The two below are NOT tuning numbers — they are
# safety valves, off by default, and they are why this is not simply a bool.
DEFAULT_STAGED = {
    "enabled": False,               # entry_style == "staged" turns the ladder on
    "deployed_ttl_minutes": 0,      # 0 = inherit the resolved entry TTL (#158)
    "max_entry_age_minutes": 0,     # 0 = off; ceiling measured from the SIGNAL (#158)
}


# ============================ config validation ===============================
# Per-key kind for the staged block. "int+" = non-negative. A tuning key from an
# older client is DROPPED by clean_staged_config rather than rejected, so a stale
# saved row cannot 422 the whole strategy on its next save.
_STAGED_SPEC = {
    "enabled": "bool",
    "deployed_ttl_minutes": "int+", "max_entry_age_minutes": "int+",
}


def _coerce(kind: str, key: str, v):
    if kind == "bool":
        if isinstance(v, bool):
            return v
        raise ValueError(f"{key} must be true or false")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number")
    if kind == "frac":
        if not (0.0 <= f <= 1.0):
            raise ValueError(f"{key} must be between 0 and 1")
        return f
    if f < 0:
        raise ValueError(f"{key} must be >= 0")
    return int(f) if kind == "int+" else f


def clean_entry_style(v) -> str:
    """Validate the entry_style enum (lower-cased). Raises ValueError otherwise."""
    s = str(v).strip().lower()
    if s not in ENTRY_STYLES:
        raise ValueError(f"entry_style must be one of {', '.join(ENTRY_STYLES)}")
    return s


def clean_staged_config(raw) -> Optional[dict]:
    """Validate + coerce a raw `staged` config block (from the UI/API). Keeps only
    known keys, coerces types, range-checks, and raises ValueError(msg) on an
    invalid value (the API translates that to a 422). Unknown keys are dropped —
    forward-compat, and backward-compat with the thirteen #250 removed. Stores
    only the keys the caller set; read-time overlay (`staged_config`) fills the
    rest from DEFAULT_STAGED."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("staged must be an object")
    out = {}
    for k, v in raw.items():
        if k not in _STAGED_SPEC or v is None:
            continue
        out[k] = _coerce(_STAGED_SPEC[k], k, v)
    return out or None


def staged_config(stored) -> dict:
    """Effective staged config: DEFAULT_STAGED overlaid with a stored block (known
    keys only). Everything reads a complete cfg through this."""
    cfg = dict(DEFAULT_STAGED)
    if isinstance(stored, dict):
        cfg.update({k: v for k, v in stored.items()
                    if k in DEFAULT_STAGED and v is not None})
    return cfg


# ============================ order-age bounds (#158) =========================
def deployed_ttl_minutes(cfg: dict, entry_ttl_minutes) -> int:
    """How long a rung's order may REST at the broker once deployed (#158).

    A rung deploys late by design, and its legs start a FRESH TTL clock at that
    moment — so with the default 60-minute entry TTL an unfilled rung can rest
    well past the point where the control account's LIMIT from the same signal is
    long gone. Configuring this makes that window a choice; 0 keeps inheriting the
    entry TTL, i.e. exactly the behaviour that shipped with #129."""
    try:
        v = int(cfg.get("deployed_ttl_minutes") or 0)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else int(entry_ttl_minutes)


def entry_age_exceeded(cfg: dict, minutes_since_signal: float) -> bool:
    """True when a staged entry has outlived `max_entry_age_minutes`, measured from
    the SIGNAL rather than from each leg's placement (#158). This is the absolute
    ceiling: it bounds the total age of a staged entry no matter how late a rung
    deployed or how its own TTL was reset. 0/unset = off (the default), so it never
    cancels anything until the operator asks for it."""
    try:
        v = int(cfg.get("max_entry_age_minutes") or 0)
    except (TypeError, ValueError):
        v = 0
    return v > 0 and minutes_since_signal > v


def entry_expiry_reason(cfg: dict, *, leg_age_minutes: float,
                        entry_age_minutes: float, entry_ttl_minutes,
                        deployed: bool) -> Optional[str]:
    """Why a still-working staged leg must be cancelled NOW, or None to let it rest.

      "max_entry_age"  the whole entry has outlived the absolute ceiling
      "leg_ttl"        this leg has outlived its own TTL

    The ceiling is checked FIRST and wins: it exists precisely to bound an entry
    whose leg-level clock was reset by a late deploy, so a leg-TTL that has not
    elapsed must not keep it alive. `deployed` marks a leg placed by a rung that
    triggered later (fresh clock -> `deployed_ttl_minutes`); everything else
    answers to the signal-time entry TTL. Pure, so the precedence is
    unit-testable (#158)."""
    if entry_age_exceeded(cfg, entry_age_minutes):
        return "max_entry_age"
    ttl = (deployed_ttl_minutes(cfg, entry_ttl_minutes) if deployed
           else int(entry_ttl_minutes or 0))
    if ttl > 0 and leg_age_minutes > ttl:
        return "leg_ttl"
    return None
