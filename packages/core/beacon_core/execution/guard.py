"""Pure execution guards — decide whether a signal may auto-execute and whether
a sized plan is within the risk limits. No DB/network here: the caller fetches
the numbers (day P&L, open risk) and passes them in, so this stays unit-testable
in isolation (see services/executor/tests/test_guard.py).
"""
from __future__ import annotations

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

    # --- Fail-safe floor: ALWAYS honored, even if the master switch is off ---
    # A mis-set `enabled: false` must never fully disarm capital protection, so
    # the manual kill-switch and the daily-loss circuit breaker apply regardless
    # of `enabled`. Added after the 2026-07-10 PM run found `risk_limits.enabled`
    # left false while the account bled well past its daily floor. Only these two
    # hard limits are unconditional; the per-signal ceiling and open-risk caps
    # below remain opt-in via the master switch (unchanged behaviour).
    if cfg.get("trading_halted"):                 # manual kill-switch
        return "trading is halted (kill switch on)"
    if daily > 0 and _dec(day_realized) <= -daily:
        return f"daily loss limit reached (today {_dec(day_realized)} <= -{daily})"

    # --- Opt-in limits: only when the master switch is on ---
    if not cfg.get("enabled"):
        return None

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
