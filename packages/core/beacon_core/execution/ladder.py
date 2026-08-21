"""The staged-entry LADDER (#250) — a table of IF/THEN rows, not thirteen knobs.

    IF                  THEN     ORDER      LEVEL        TARGET
    signal arrives      open     POSITION   ENTRY-FROM   TP1
    price reaches MID   open     POSITION   MID          TP2
    price reaches MID   open     STOP       ENTRY-FROM   TP3
    price reaches TP1   cancel   —          —            —

That table IS the configuration. Every row is one order: when to place it, what
kind, at which level, for which target. Read it top to bottom and you know what
the account will do — which is the thing thirteen tuning numbers could never tell
anyone.

ONE TABLE COVERS EVERY SIGNAL SHAPE. #250 wrote out four ladders (1-level vs
2-level entry x 2/3/4+ TPs), but they collapse into one:

  * `ENTRY-TO` on a single-level signal IS `ENTRY-FROM` — the same number — so a
    row referencing the far edge simply lands on the near one and the 1-level
    ladder falls out of the 2-level one for free.
  * A row whose TARGET the signal does not have is NOT CREATED. Never an error,
    never substituted with a nearer TP: a 3-TP signal run against a table with a
    TP4 row places the other rows and skips that one.

TOTAL RISK EQUALS THE SINGLE-SHOT PLAN. Every rung is planned and sized UP FRONT,
before the first order goes out, against the risk the ordinary fanout would have
taken on the same signal. So if every rung fills and price then runs to the stop,
the loss is the same money the single-shot entry would have lost — the ladder
changes WHEN size arrives, never HOW MUCH. Sizing a rung at the moment it
triggers would instead let a signal that walks to MID and back stack risk without
limit, which is the one way this feature could quietly cost more than it says.

The arithmetic is `size_legs`' own, applied to a budget that is measured rather
than assumed (see `single_shot_risk`), so the guarantee survives any allocation
mode rather than only the `even` one every account happens to use today.

PURE — stdlib + the planner's dataclasses. Nothing here places an order; it
returns legs and the price level each one waits for. The monitor carries them out.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import List, Optional

from .planner import PlannedLeg, build_plan

# --- the IF column ------------------------------------------------------------
WHEN_SIGNAL = "signal"        # the moment the signal arrives
WHEN_MID = "mid"              # price reaches MID (midpoint of far entry -> SL)
WHEN_TP1 = "tp1"              # price reaches TP1
WHENS = (WHEN_SIGNAL, WHEN_MID, WHEN_TP1)

# --- the THEN column ----------------------------------------------------------
DO_OPEN = "open"
DO_CANCEL = "cancel_all"      # pull every other order for this signal
ACTIONS = (DO_OPEN, DO_CANCEL)

# --- the ORDER column ---------------------------------------------------------
ORDER_POSITION = "POSITION"   # take it now, at the market
ORDER_LIMIT = "LIMIT"         # rest better than the market
ORDER_STOP = "STOP"           # rest worse than the market; fills on continuation
ORDERS = (ORDER_POSITION, ORDER_LIMIT, ORDER_STOP)

# --- the LEVEL column ---------------------------------------------------------
LVL_ENTRY_FROM = "ENTRY_FROM"
LVL_ENTRY_TO = "ENTRY_TO"
LVL_MID = "MID"
LEVELS = (LVL_ENTRY_FROM, LVL_ENTRY_TO, LVL_MID)


def _row(when, action, order=None, level=None, target=None) -> dict:
    r = {"when": when, "action": action}
    if action == DO_OPEN:
        r.update({"order": order, "level": level, "target": int(target)})
    return r


# The table an operator starts from: #250's 3-TP ladder, which degrades to its
# 2-TP one on a 2-TP signal because the TP3 row is then not created.
DEFAULT_LADDER = [
    _row(WHEN_SIGNAL, DO_OPEN, ORDER_POSITION, LVL_ENTRY_FROM, 1),
    _row(WHEN_MID, DO_OPEN, ORDER_POSITION, LVL_MID, 2),
    _row(WHEN_MID, DO_OPEN, ORDER_STOP, LVL_ENTRY_FROM, 3),
    _row(WHEN_TP1, DO_CANCEL),
]


# ============================ validation ======================================
def clean_ladder(raw) -> Optional[List[dict]]:
    """Validate a ladder table from the UI/API. Raises ValueError(msg) — which the
    API turns into a 422 — rather than dropping a malformed row, because a ladder
    silently missing a rung is a different strategy from the one that was saved."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError("ladder must be a list of rows")
    out: List[dict] = []
    for i, r in enumerate(raw, start=1):
        where = f"ladder row {i}"
        if not isinstance(r, dict):
            raise ValueError(f"{where} must be an object")
        when = str(r.get("when") or "").strip().lower()
        if when not in WHENS:
            raise ValueError(f"{where}: 'when' must be one of {', '.join(WHENS)}")
        action = str(r.get("action") or "").strip().lower()
        if action not in ACTIONS:
            raise ValueError(f"{where}: 'action' must be one of {', '.join(ACTIONS)}")
        if action == DO_CANCEL:
            out.append({"when": when, "action": action})
            continue
        order = str(r.get("order") or "").strip().upper()
        if order not in ORDERS:
            raise ValueError(f"{where}: 'order' must be one of {', '.join(ORDERS)}")
        level = str(r.get("level") or "").strip().upper()
        if level not in LEVELS:
            raise ValueError(f"{where}: 'level' must be one of {', '.join(LEVELS)}")
        try:
            target = int(r.get("target"))
        except (TypeError, ValueError):
            raise ValueError(f"{where}: 'target' must be a TP number (got "
                             f"{r.get('target')!r})")
        if target < 1:
            raise ValueError(f"{where}: 'target' must be 1 or more")
        out.append({"when": when, "action": action, "order": order,
                    "level": level, "target": target})
    if not out:
        return None
    if not any(r["when"] == WHEN_SIGNAL for r in out):
        raise ValueError("a ladder needs at least one row that fires when the "
                         "signal arrives, or nothing is ever opened")
    return out


# ============================ geometry ========================================
def mid_level(entry_to, sl) -> Decimal:
    """MID — halfway from the FAR entry edge to the stop (#250).

    The far edge, so a zone signal measures MID from the deepest price it was
    willing to enter at, and a single-level signal measures it from its only
    entry (`entry_to == entry_from` there)."""
    return (Decimal(str(entry_to)) + Decimal(str(sl))) / Decimal(2)


def resolve_level(name: str, sig) -> Optional[Decimal]:
    if name == LVL_ENTRY_FROM:
        return Decimal(str(sig.entry_from))
    if name == LVL_ENTRY_TO:
        return Decimal(str(sig.entry_to))
    if name == LVL_MID:
        return mid_level(sig.entry_to, sig.sl)
    return None


def reached(direction: str, price, level) -> bool:
    """Has price arrived at an ENTRY-side `level` — one it approaches from the
    losing side? A BUY ladder waits for price to FALL to MID; a SELL ladder for
    it to rise. Wrong for a take-profit, which sits the other way: see
    `reached_target`."""
    price, level = Decimal(str(price)), Decimal(str(level))
    return price <= level if direction == "BUY" else price >= level


def reached_target(direction: str, price, tp) -> bool:
    """Has price reached a TAKE-PROFIT — moved in the WINNING direction?

    The mirror of `reached`, and not interchangeable with it. A BUY's TP1 sits
    ABOVE the entry, so asking `reached` about it is true from the first tick
    (price is already below TP1) and a `cancel everything when TP1 is reached`
    row would fire the instant the trade opened, cancelling the ladder before it
    ever ran."""
    price, tp = Decimal(str(price)), Decimal(str(tp))
    return price >= tp if direction == "BUY" else price <= tp


# ============================ planning ========================================
def plan_ladder(sig, rows: Optional[List[dict]] = None, *,
                max_tp_distance_pct=None, min_stop_distance=None) -> List[PlannedLeg]:
    """One PlannedLeg per `open` row the signal can actually support.

    `tranche` carries the row's trigger (`signal` / `mid` / `tp1`) so the monitor
    can group by what it is waiting for, and `trigger` carries the price level
    that arms it — None for a rung that goes out immediately.

    Rows are DROPPED, not failed, when the signal cannot support them: a target
    the signal has no TP for, geometry that would put the stop on the wrong side
    of that rung's own entry, or a TP the entry guards would have thrown away.
    `cancel_all` rows produce no leg; they are instructions to the monitor, and
    `cancel_rows` reads them back out.

    `max_tp_distance_pct` and `min_stop_distance` are the SAME guards `build_plan`
    applies (planner.py), passed through so a laddered account drops the same TPs
    the single-shot one does. Without them a parse artifact — tp=1530 on gold near
    4180 — becomes a rung, and the two entry styles stop being comparable on the
    very signals the guard exists to throw away (#152)."""
    rows = rows or DEFAULT_LADDER
    tps = list(sig.tps or [])
    legs: List[PlannedLeg] = []
    for r in rows:
        if r.get("action") != DO_OPEN:
            continue
        idx = int(r["target"])
        if idx > len(tps):
            continue                     # the signal has no such TP -> no order
        entry = resolve_level(r["level"], sig)
        if entry is None:
            continue
        tp = tps[idx - 1]
        # The same two entry guards build_plan applies, against THIS rung's entry.
        if min_stop_distance is not None and abs(Decimal(str(tp)) - entry) < Decimal(str(min_stop_distance)):
            continue                     # tp within the broker's minimum distance
        if (max_tp_distance_pct is not None and entry
                and abs(Decimal(str(tp)) - entry) / abs(entry) > Decimal(str(max_tp_distance_pct))):
            continue                     # a parse artifact, not a real target
        # The stop must protect this rung's own entry, and the target must still
        # be ahead of it — a MID rung on a signal whose TP1 sits between MID and
        # the entry would otherwise be born already past its target.
        if sig.direction == "BUY":
            if not (Decimal(str(sig.sl)) < entry < Decimal(str(tp))):
                continue
        else:
            if not (Decimal(str(tp)) < entry < Decimal(str(sig.sl))):
                continue
        legs.append(PlannedLeg(
            side=sig.direction, entry=entry, tp=Decimal(str(tp)),
            sl=Decimal(str(sig.sl)), tp_index=idx, order_type=r["order"],
            tranche=r["when"],
            trigger=None if r["when"] == WHEN_SIGNAL else entry))
    return legs


def cancel_rows(rows: Optional[List[dict]] = None) -> List[str]:
    """The triggers on which the monitor must pull every remaining order."""
    return [r["when"] for r in (rows or DEFAULT_LADDER) if r.get("action") == DO_CANCEL]


# ============================ sizing ==========================================
def single_shot_risk(sig, *, current_price, equity, risk, instrument,
                     fx_factor=Decimal(1), **plan_kw) -> Decimal:
    """What the ORDINARY fanout would have risked on this signal, in account
    currency — the number the ladder has to match.

    Measured by actually building and sizing the single-shot plan rather than
    reasoning about the risk config, so it stays correct under `per_tp`, under
    the zone-doubling `per_tp_split_across_entries` exists to undo (#154), and
    under whatever allocation mode arrives next."""
    from ..risk.sizing import size_legs, plan_total_risk
    plan = build_plan(sig, current_price=current_price, **plan_kw)
    size_legs(plan.legs, equity=equity, risk=risk, instrument=instrument,
              fx_factor=fx_factor)
    return plan_total_risk(plan.legs)


def size_ladder(legs: List[PlannedLeg], *, budget: Decimal, instrument,
                fx_factor=Decimal(1)) -> Decimal:
    """Size every rung so the whole ladder risks `budget` and no more.

    Deliberately `size_legs` with a FIXED-CASH, EVEN allocation: the budget is
    already the measured single-shot total, so the only thing left is to divide
    it across the rungs. Each rung's lot still comes from its own entry-to-stop
    distance, so a rung at MID — closer to the stop — carries a bigger lot for
    the same money, which is the point of entering there.

    Lots round DOWN, exactly as the single-shot path rounds them, so the total
    lands at or just under budget and never above it. Returns the total."""
    from ..risk.sizing import RiskConfig, size_legs, plan_total_risk
    size_legs(legs, equity=Decimal(0),
              risk=RiskConfig(basis="fixed_cash", value=Decimal(str(budget)),
                              allocation="even"),
              instrument=instrument, fx_factor=fx_factor)
    return plan_total_risk(legs)
