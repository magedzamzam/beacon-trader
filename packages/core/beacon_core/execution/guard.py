"""Pure execution guards — decide whether a signal may auto-execute and whether
a sized plan is within the risk limits. No DB/network here: the caller fetches
the numbers (day P&L, open risk) and passes them in, so this stays unit-testable
in isolation (see services/executor/tests/test_guard.py).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple

# Source names containing any of these never auto-execute live orders.
EXECUTION_NAME_BLOCKLIST = ("test", "sample", "demo")

# Fail-safe defaults used when the `risk_limits` setting is missing entirely, so
# an un-configured install never trades with no brakes (see #19). Values are in
# the account currency.
DEFAULT_RISK_LIMITS = {
    "enabled": True,
    "trading_halted": False,
    "daily_loss_limit": 500,
    "per_signal_max_pct_of_daily": 0.5,
    "max_open_risk_per_account": 2500,
    "max_open_risk_per_symbol": 2500,
    # Per-signal risk cap (#78): a single signal's summed worst-case-to-SL risk
    # (all entry×TP legs) may not exceed this % of account equity — legs are
    # scaled down proportionally if it would. Bounds per_tp leg-stacking at the
    # source. 0 disables. Sized above a normal even/1% plan so it only bites
    # stacked fanouts.
    "max_signal_risk_pct": 2.0,
    # Graduated daily-loss breaker (#126): a SOFT limit below the hard
    # `daily_loss_limit` that PAUSES new entries (existing positions keep
    # managing) instead of halting the whole day. 0 disables it (default) → the
    # breaker is a single hard halt, exactly as before. `breaker_cooldown_minutes`
    # arms a timed pause that outlasts a P&L recovery; 0 = pause only while the
    # day's realized stays in the soft zone (auto-resume when winners recover it).
    "daily_soft_loss_limit": 0,
    "breaker_cooldown_minutes": 0,
}


def should_auto_execute(*, enabled_for_trading: bool, is_trusted: bool,
                        name: Optional[str],
                        allow_untrusted: bool = False) -> Tuple[bool, Optional[str]]:
    """(ok, reason). ok=False means do NOT place live orders for this source."""
    if not enabled_for_trading:
        return (False, "source not enabled for trading")
    if any(tok in (name or "").lower() for tok in EXECUTION_NAME_BLOCKLIST):
        return (False, "source name is blocklisted (test/sample/demo)")
    if not is_trusted and not allow_untrusted:
        return (False, "source is not trusted")
    return (True, None)


def _dec(v, default="0") -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def risk_limit_reason(*, planned_risk, day_realized, open_risk_symbol,
                      open_risk_account, cfg: Optional[dict]) -> Optional[str]:
    """Return a block reason if placing `planned_risk` would violate a limit,
    else None. All money values are in the ACCOUNT currency (same as
    `trades.planned_risk` / `trades.realized_pl`).

    cfg keys (all optional; DB-backed `risk_limits` setting):
      enabled                     master switch
      daily_loss_limit            magnitude, account ccy (e.g. 500 == -500 floor)
      per_signal_max_pct_of_daily per-signal risk ceiling as a fraction of it
      max_open_risk_per_symbol    cap on summed open planned_risk for the symbol
      max_open_risk_per_account   cap on summed open planned_risk for the account
    """
    cfg = cfg or {}
    pr = _dec(planned_risk)
    daily = abs(_dec(cfg.get("daily_loss_limit")))

    # --- Kill-switch: the one explicit "STOP" flag; halts whenever set, so a big
    # red button is never silently disarmed by the master switch. (#65 keeps this
    # explicit-flag-gated; nothing else is unconditional.)
    if cfg.get("trading_halted"):
        return "trading is halted (kill switch on)"

    # --- Master switch (#65): when the operator has explicitly turned limits OFF,
    # NOTHING below blocks — the daily-loss floor and every cap are opt-in. A
    # MISSING `risk_limits` row is the only fail-safe case (#19): the caller then
    # passes DEFAULT_RISK_LIMITS, which has `enabled: True`, so an un-configured
    # install is still protected. Present + enabled:false honours the operator.
    if not cfg.get("enabled"):
        return None

    # --- Daily-loss circuit breaker (only when enabled; daily_loss_limit:0 = off) ---
    if daily > 0 and _dec(day_realized) <= -daily:
        return f"daily loss limit reached (today {_dec(day_realized)} <= -{daily})"

    pct = cfg.get("per_signal_max_pct_of_daily")
    if daily > 0 and pct:
        ceiling = daily * _dec(pct)
        if pr > ceiling:
            return (f"per-signal risk {pr} exceeds ceiling {ceiling} "
                    f"({_dec(pct) * 100}% of daily limit {daily})")

    cap_sym = cfg.get("max_open_risk_per_symbol")
    if cap_sym and (_dec(open_risk_symbol) + pr) > _dec(cap_sym):
        return (f"open risk on this symbol would reach "
                f"{_dec(open_risk_symbol) + pr}, over cap {_dec(cap_sym)}")

    cap_acct = cfg.get("max_open_risk_per_account")
    if cap_acct and (_dec(open_risk_account) + pr) > _dec(cap_acct):
        return (f"open account risk would reach {_dec(open_risk_account) + pr}, "
                f"over cap {_dec(cap_acct)}")
    return None


# --- Graduated daily-loss circuit breaker (#126) ------------------------------
# The single hard "-N -> halt-for-the-day" is redesigned into three states:
#   ok       -> trade normally
#   cooldown -> SOFT breach: pause NEW entries (existing positions keep managing),
#               either for a timed window or while the day stays in the soft zone
#   halt     -> HARD breach: halt for the day (the existing risk_limit_reason path)
# `daily_loss_state` is the pure classifier; `soft_breaker_decision` adds the
# cooldown timing. The HARD halt itself stays in risk_limit_reason unchanged, so
# these are ADDITIVE — with daily_soft_loss_limit=0 (default) they are inert and
# behaviour is byte-identical to the single hard halt.
#
# BASIS: `day_realized` must be broker-truth realized (deduped position_activities),
# NOT the ledger sum of trades.realized_pl, which over-states the loss (#74) and
# trips the breaker ~30-50% early. Feeding the honest basis is caller-side work.

def daily_loss_state(day_realized, cfg: Optional[dict]) -> str:
    """'halt' | 'soft' | 'ok' from the day's realized loss vs the hard
    (`daily_loss_limit`) and soft (`daily_soft_loss_limit`) thresholds. Pure,
    deterministic, and account-agnostic — the same cfg + basis yields the same
    state on every A/B account (the symmetry the exit experiment needs)."""
    cfg = cfg or {}
    r = _dec(day_realized)
    hard = abs(_dec(cfg.get("daily_loss_limit")))
    soft = abs(_dec(cfg.get("daily_soft_loss_limit")))
    if hard > 0 and r <= -hard:
        return "halt"
    if soft > 0 and r <= -soft:
        return "soft"
    return "ok"


def soft_breaker_decision(*, day_realized, cfg: Optional[dict], now,
                          cooldown_until=None) -> dict:
    """Graduated SOFT breaker + cooldown (#126). The HARD halt stays in
    risk_limit_reason; this covers ONLY the soft band, so it never double-counts
    the hard halt. Pure: the caller passes `now` and the persisted `cooldown_until`
    (tz-aware datetimes), persists the returned `cooldown_until`, and logs a
    `breaker_state` event when `state` changes.

    Returns {state, block, reason, cooldown_until}:
      state 'ok' | 'cooldown'
      block True  -> pause NEW entries (existing positions keep managing)
      A soft breach arms/extends a `breaker_cooldown_minutes` window (0 = pause
      only while realized stays in the soft zone). Inert when the master switch is
      off or daily_soft_loss_limit=0."""
    cfg = cfg or {}
    if not cfg.get("enabled"):
        return {"state": "ok", "block": False, "reason": None, "cooldown_until": None}
    soft = abs(_dec(cfg.get("daily_soft_loss_limit")))
    if soft <= 0:
        return {"state": "ok", "block": False, "reason": None, "cooldown_until": None}
    r = _dec(day_realized)
    try:
        mins = int(_dec(cfg.get("breaker_cooldown_minutes")))
    except (TypeError, ValueError):
        mins = 0
    armed = cooldown_until
    if r <= -soft:
        block = True
        if mins > 0:
            armed = now + timedelta(minutes=mins)      # arm/extend a timed pause
    else:
        block = armed is not None and now < armed      # timed window still open
        if not block:
            armed = None
    if block:
        tail = (f"; paused until {armed.isoformat()}" if armed is not None
                else "; new entries paused")
        return {"state": "cooldown", "block": True, "cooldown_until": armed,
                "reason": f"daily soft-loss cooldown (today {r} <= -{soft}){tail}"}
    return {"state": "ok", "block": False, "reason": None, "cooldown_until": None}
