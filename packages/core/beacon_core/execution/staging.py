"""Confirmation-staged entry — the pure engine (#129).

Instead of resting a signal's whole fanout at once, deploy it in TRANCHES so a
straight-to-SL move is caught on PARTIAL size while a genuine pullback-and-continue
still gets full size. Same signal, same SL — only *when/if* each leg deploys changes.

INTENDED TOTAL RISK vs a control account (#154): matched under
`allocation="even"` (the budget is split across whatever legs exist), but NOT
under `allocation="per_tp"` unless `per_tp_split_across_entries` is on. A zone
signal makes the single-shot planner emit TWO legs per tp_index (one per zone
edge) and per_tp charges each the full percent, while this module emits ONE leg
per tp_index — so the control account stakes ~2x. See `risk/sizing.py`.

    EXECUTE(T1)  ->  MONITOR  ->  DECIDE(T2 / reclaim)  ->  EXECUTE

Three tranche roles (a signal's legs are partitioned across them by TP index):
  toe_in   deploy immediately at first touch of the NEAR zone edge (the executor
           places these — the "toe-in").
  runner   deploy when price reaches the DEEP zone edge (entry_to) — better R:R.
  reclaim  the deferred tail. NOT rested as limits: if price breaks beyond the
           deep edge and then RECLAIMS it (momentum confirmation) a STOP re-entry
           fills; on a no-mercy plunge that never reclaims, they never fill and
           the account is stopped on partial size.

THE DECIDE ENGINE
-----------------
`decide_tranche()` is the seam the operator asked for: a mechanical base decision
(pure price/TTL geometry) composed with a pipeline of pluggable **modifier
deciders**. A modifier may only make a decision MORE conservative — turn a DEPLOY
into a SKIP, or shrink its size_factor — never more aggressive (it can't force a
deploy or up-size). That invariant is enforced in `_apply_modifier`, so wiring a
new signal later (e.g. "counter-trend pullback into the deep edge is riskier ->
skip the runner") is a matter of appending a decider to `DECIDERS`, with no risk
of a new input accidentally opening a trade it shouldn't.

This module is PURE (stdlib only, no DB/broker/clock) so it runs on a bare box
and is exhaustively unit-testable on synthetic price paths. The monitor supplies
the live context each tick and carries out whatever the decision says; nothing
here touches an order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

# The entry-order styles a strategy may choose. "staged" turns this engine on;
# "market"/"limit" are the existing single-shot behaviours (the planner default).
ENTRY_STYLES = ("market", "limit", "staged")

# --- tranche roles ------------------------------------------------------------
TOE_IN = "toe_in"
RUNNER = "runner"
RECLAIM = "reclaim"
ROLES = (TOE_IN, RUNNER, RECLAIM)

# --- tranche states (persisted per tranche on the sidecar row) ----------------
PENDING = "pending"     # not yet deployed
DEPLOYED = "deployed"   # a LIMIT/MARKET order is working/open for this tranche
ARMED = "armed"         # reclaim only: a STOP is resting at the broker
FILLED = "filled"       # at least one leg filled
EXPIRED = "expired"     # TTL elapsed without deploying/filling
SKIPPED = "skipped"     # a decider vetoed the deployment

# --- decision actions ---------------------------------------------------------
WAIT = "wait"           # do nothing this tick
DEPLOY = "deploy"       # place the tranche's order(s) now (see `mode`)
EXPIRE = "expire"       # cancel/abandon the tranche (TTL)
SKIP = "skip"           # a modifier vetoed a would-be deploy

# --- order modes a DEPLOY may ask for -----------------------------------------
MODE_LIMIT = "LIMIT"
MODE_MARKET = "MARKET"
MODE_STOP = "STOP"      # reclaim re-entry (fills when price trades back through)


# Defaults grounded in the gold dumps (1h ATR ~12-16, zone widths ~$3-4, deep->SL
# stops ~$6-12, TP ladders 2-5 deep). All tunable per (account, source) from the UI.
DEFAULT_STAGED = {
    "enabled": False,               # master flag; entry_style == "staged" turns it on
    # --- partition (by_tp_tier): nearest TP -> toe-in, FARTHEST TP -> runner
    #     (best R:R), the MIDDLE TPs -> the deferred reclaim tail. Self-scaling:
    #     deeper ladders defer more size. ---
    "toe_in_tps": 1,                # count of nearest TP tiers deployed immediately
    "runner_tps": 1,                # count of farthest TP tiers deployed at the deep edge
    "max_deferred_fraction": 0.60,  # cap the deferred share of the ladder
    "min_deferred_fraction": 0.20,  # ensure a tail exists (2-TP -> defer TP2)
    # --- break-then-reclaim geometry (ATR-relative, with clamps + cash fallback) ---
    "reclaim_break_atr": 0.35,      # price must trade this far beyond the deep edge to arm (~$5 @ ATR14)
    "reclaim_break_cash": 0.0,      # used only when ATR is unavailable (0 = disabled)
    "reclaim_break_max_frac_of_stop": 0.5,   # never arm past half-way to SL (protects R)
    "reclaim_break_abs_cap": 8.0,   # hard $ cap (guards ATR-spike regimes)
    "stop_offset_atr": 0.10,        # offset the reclaim STOP trigger beyond the deep edge (~$1.5)
    # --- per-tranche TTLs (minutes), each measured from when the tranche entered
    #     its current wait state (the reclaim-armed clock starts on ARM, not open) ---
    "runner_ttl_minutes": 45,       # give up waiting for the deep-edge touch (< the 60 entry TTL)
    "reclaim_pending_ttl_minutes": 60,   # give up waiting for a break beyond deep
    "reclaim_armed_ttl_minutes": 60,     # give up waiting for the reclaim fill
    # --- how long a tranche's order may REST once deployed, and the absolute age
    #     of the whole staged entry (#158). A tranche deploys late by design, and
    #     its legs get a fresh TTL clock from that moment — so an unfilled staged
    #     order can outlive a control account's by the pending wait. These two make
    #     that window deliberate instead of inherited:
    "deployed_ttl_minutes": 0,      # 0 = inherit the resolved entry TTL (today's behaviour)
    "max_entry_age_minutes": 0,     # 0 = off; hard ceiling measured from the SIGNAL,
                                    # expires any still-working staged leg past it
    # --- guardrail: below this the stop is too tight to stage a break around;
    #     the caller falls back to a single-shot entry. ---
    "min_stop_atr": 0.5,
}


# ============================ config validation ===============================
# Per-key kind for the staged block. "frac" = 0..1, "float+"/"int+" = non-negative.
_STAGED_SPEC = {
    "enabled": "bool",
    "toe_in_tps": "int+", "runner_tps": "int+",
    "max_deferred_fraction": "frac", "min_deferred_fraction": "frac",
    "reclaim_break_atr": "float+", "reclaim_break_cash": "float+",
    "reclaim_break_max_frac_of_stop": "frac", "reclaim_break_abs_cap": "float+",
    "stop_offset_atr": "float+",
    "runner_ttl_minutes": "int+", "reclaim_pending_ttl_minutes": "int+",
    "reclaim_armed_ttl_minutes": "int+", "min_stop_atr": "float+",
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
    invalid value (the API translates that to a 422). Unknown keys are dropped
    (forward-compat). Stores only the keys the caller set — read-time overlay
    (`staged_config`) fills the rest from DEFAULT_STAGED."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("staged must be an object")
    out = {}
    for k, v in raw.items():
        if k not in _STAGED_SPEC or v is None:
            continue
        out[k] = _coerce(_STAGED_SPEC[k], k, v)
    lo, hi = out.get("min_deferred_fraction"), out.get("max_deferred_fraction")
    if lo is not None and hi is not None and lo > hi:
        raise ValueError("min_deferred_fraction cannot exceed max_deferred_fraction")
    return out or None


def staged_config(stored) -> dict:
    """Effective staged config: DEFAULT_STAGED overlaid with a stored block (known
    keys only). The engine always reads a complete cfg through this."""
    cfg = dict(DEFAULT_STAGED)
    if isinstance(stored, dict):
        cfg.update({k: v for k, v in stored.items()
                    if k in DEFAULT_STAGED and v is not None})
    return cfg


# ============================ geometry helpers ================================
def zone_edges(direction: str, entry_a, entry_b) -> tuple:
    """(near_edge, deep_edge) for a zone. The NEAR edge is the one price reaches
    first; the DEEP edge is the better (further) fill. For a BUY (dip-buy, zone
    below price) near is the higher price; for a SELL it's the lower."""
    a, b = float(entry_a), float(entry_b)
    hi, lo = max(a, b), min(a, b)
    return (hi, lo) if direction == "BUY" else (lo, hi)


def _reached(direction: str, price: float, level: float) -> bool:
    """Has price reached `level` in the fill direction? BUY fills on a fall to the
    level; SELL on a rise to it."""
    return price <= level if direction == "BUY" else price >= level


def beyond_deep(direction: str, price: float, deep_edge: float) -> float:
    """How far price sits BEYOND the deep edge in the adverse direction, >= 0.
    For a BUY that's how far below deep; for a SELL how far above. 0 when price is
    at/short-of the deep edge. The monitor accumulates the MAX of this per trade
    into `max_adverse_beyond_deep`."""
    gap = (deep_edge - price) if direction == "BUY" else (price - deep_edge)
    return gap if gap > 0.0 else 0.0


def reclaim_stop_level(direction: str, deep_edge: float, atr: Optional[float],
                       cfg: dict) -> float:
    """Trigger level for the reclaim STOP: the deep edge, optionally offset by
    `stop_offset_atr` so a fill needs a slightly stronger reclaim. Offset is added
    in the profit/fill direction (BUY STOP above deep, SELL STOP below deep)."""
    off = float(cfg.get("stop_offset_atr") or 0.0) * float(atr) if atr else 0.0
    return deep_edge + off if direction == "BUY" else deep_edge - off


def break_distance(cfg: dict, atr: Optional[float],
                   sl_dist: Optional[float] = None) -> Optional[float]:
    """The 'break' distance in price units: `reclaim_break_atr * ATR` when ATR is
    known, else the `reclaim_break_cash` fallback. None when neither is usable — a
    fail-safe that keeps the reclaim tranche from arming without a defined break.

    Two clamps keep it sane across regimes: never more than
    `reclaim_break_max_frac_of_stop` of the deep-edge→SL distance (so a re-entry is
    never armed past half-way to the stop), and never more than the absolute
    `reclaim_break_abs_cap` (guards 4h/daily ATR spikes)."""
    m = cfg.get("reclaim_break_atr")
    if atr and m:
        base = float(atr) * float(m)
    else:
        c = cfg.get("reclaim_break_cash")
        base = float(c) if c else None
    if base is None:
        return None
    frac = cfg.get("reclaim_break_max_frac_of_stop")
    if frac and sl_dist:
        base = min(base, float(frac) * float(sl_dist))
    cap = cfg.get("reclaim_break_abs_cap")
    if cap:
        base = min(base, float(cap))
    return base


def deployed_ttl_minutes(cfg: dict, entry_ttl_minutes) -> int:
    """How long a tranche's order may REST at the broker once deployed (#158).

    A tranche deploys late by design, and its legs start a FRESH TTL clock at that
    moment — so with the default 60-minute entry TTL an unfilled runner (up to 45
    pending) can rest until ~T+105 and an armed reclaim until ~T+120, where the
    control account's LIMIT from the same signal is gone at T+60. Configuring this
    makes that window a choice; 0 keeps inheriting the entry TTL, i.e. exactly the
    behaviour that shipped with #129."""
    try:
        v = int(cfg.get("deployed_ttl_minutes") or 0)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else int(entry_ttl_minutes)


def entry_age_exceeded(cfg: dict, minutes_since_signal: float) -> bool:
    """True when a staged entry has outlived `max_entry_age_minutes`, measured from
    the SIGNAL rather than from each leg's placement (#158). This is the absolute
    ceiling: it bounds the total age of a staged entry no matter how late a tranche
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
    elapsed must not keep it alive. `deployed` marks a leg placed by a tranche
    deploy (fresh clock -> `deployed_ttl_minutes`); everything else answers to the
    signal-time entry TTL. Pure so the precedence is unit-testable (#158)."""
    if entry_age_exceeded(cfg, entry_age_minutes):
        return "max_entry_age"
    ttl = (deployed_ttl_minutes(cfg, entry_ttl_minutes) if deployed
           else int(entry_ttl_minutes or 0))
    if ttl > 0 and leg_age_minutes > ttl:
        return "leg_ttl"
    return None


def stop_too_tight(sl_dist: float, atr: Optional[float], cfg: dict) -> bool:
    """True when |entry-SL| is below `min_stop_atr` ATRs — too tight to bother
    staging a break-then-reclaim around (the caller falls back to a plain entry)."""
    m = cfg.get("min_stop_atr")
    if not (atr and m):
        return False
    return sl_dist < float(m) * float(atr)


# ============================ partition =======================================
def partition_tps(tps: list, cfg: dict) -> dict:
    """`by_tp_tier` partition of the TP ladder (1-based indices, near→far) across
    the three roles: the NEAREST `toe_in_tps` are the toe-in, the FARTHEST
    `runner_tps` are the runner (best R:R), and everything in the MIDDLE is the
    deferred reclaim tail. Self-scaling — deeper ladders defer more size. Returns
    {toe_in:[idx...], runner:[idx...], reclaim:[idx...]} (disjoint, covering 1..n).

    Two guardrails bound the tail:
      - min_deferred_fraction: a 2-TP signal (toe=[1], runner=[2], reclaim=[])
        reassigns the farthest leg to the tail so staging still buys protection.
      - max_deferred_fraction: caps the deferred share; the excess NEAREST reclaim
        legs fall back to the runner (deploy at the deep edge instead of deferring).
    """
    n = len(tps or [])
    if n == 0:
        return {TOE_IN: [], RUNNER: [], RECLAIM: []}
    idx = list(range(1, n + 1))
    if n == 1:
        return {TOE_IN: [1], RUNNER: [], RECLAIM: []}          # all toe-in

    t = max(1, int(cfg.get("toe_in_tps", 1)))
    r = max(0, int(cfg.get("runner_tps", 1)))
    toe = idx[:t]
    run = [i for i in idx[max(t, n - r):]] if r > 0 else []    # farthest r, no overlap with toe
    rec = [i for i in idx if i not in toe and i not in run]

    # min tail: if the tier split left no deferred legs, move the farthest runner
    # leg into the tail (the 2-TP case) so there is always something to confirm.
    min_frac = float(cfg.get("min_deferred_fraction", 0.0) or 0.0)
    if not rec and run and (min_frac > 0.0):
        rec = [run[-1]]
        run = run[:-1]

    # max tail: cap the deferred share; push the excess NEAREST reclaim legs back
    # to the runner so no more than the cap is gated behind a reclaim.
    max_frac = float(cfg.get("max_deferred_fraction", 1.0) or 1.0)
    if max_frac < 1.0 and rec:
        max_rec = int(max_frac * n + 1e-9)
        while len(rec) > max(1, max_rec):
            run.append(rec.pop(0))                              # nearest reclaim -> runner
        run.sort()
    return {TOE_IN: toe, RUNNER: run, RECLAIM: rec}


def _D(x):
    from decimal import Decimal
    return x if isinstance(x, Decimal) else Decimal(str(x))


def build_staged_legs(*, direction: str, tps: list, near_edge, deep_edge, sl, atr,
                      current_price, cfg: dict, min_stop_distance=None,
                      market_hint: bool = False, chase_tolerance_r=0.25,
                      chase_tolerance_atr=0.0, beyond_tolerance: str = "limit",
                      max_tp_distance_pct=None, candle_high=None,
                      candle_low=None) -> list:
    """Partition the TP ladder into tranches and produce ONE PlannedLeg per TP tier,
    each tagged with its tranche + deploy level + order mode:
      toe_in  -> near edge; MARKET if price already crossed it, else LIMIT (deploy now)
      runner  -> deep edge; LIMIT (the monitor deploys it when price reaches deep)
      reclaim -> reclaim STOP trigger (deep edge +/- offset); STOP (monitor arms on break)

    ENTRY-GUARD PARITY WITH `build_plan`
    ------------------------------------
    A staged account is only comparable to a single-shot control account if both
    take the SAME signals, drop the SAME legs, and treat "the market already got
    there" the same way. Every guard below therefore mirrors `build_plan` exactly
    and reuses its helpers rather than restating the geometry:

      `market_hint`         the signal's "enter now" hint AND the account's
                            `honor_market_hint` (the caller ANDs them, as
                            build_plan does). Set -> a toe-in whose near edge price
                            has NOT reached deploys MARKET at the live price,
                            bounded by the chase tolerance (#67/#151).
      `beyond_tolerance`    what a MARKET-hint entry beyond that tolerance does:
                            "limit" (default) rests at the near edge; "skip" trades
                            NOTHING — and because a staged trade with no toe-in is
                            not the strategy under test, the runner and reclaim
                            tranches are skipped with it (#155).
      `candle_high/low`     the current candle's range. An entry the candle has
                            already touched counts as reached even if price has
                            since retraced, exactly as build_plan's crossed test
                            (candle range UNION live price) (#153).
      `max_tp_distance_pct` drops a TP an implausible distance from its deploy
                            level — the parse-artifact guard (#152).

    Runner + reclaim staging are otherwise unaffected: they stay relative to the
    zone.

    TTL semantics across the two arms (#157/#158): a single-shot LIMIT expires
    `entry_ttl_minutes` after PLACEMENT. A staged tranche deploys late by design
    and its legs start a fresh clock then, so a staged order rests longer in
    wall-clock terms. That window is now explicit — `deployed_ttl_minutes` sets
    it (0 = inherit the entry TTL, the shipped behaviour) and
    `max_entry_age_minutes` caps the total age of the entry measured from the
    SIGNAL (0 = off). See `deployed_ttl_minutes()` / `entry_age_exceeded()`.

    Pure; returns a list[PlannedLeg] UNSIZED (the caller runs risk.size_legs off each
    leg's `entry`). A leg whose geometry is broken (TP not beyond its deploy level,
    SL on the wrong side) is marked invalid with a reason, mirroring build_plan."""
    # Reuse the baseline's exact chase/cross geometry rather than restating it — A
    # and C must agree on what "close enough to enter now" means or the arms diverge.
    from .planner import (PlannedLeg, _chase_gap, _chase_tolerance,
                          _entry_crossed)
    part = partition_tps(tps, cfg)
    near, deep, slD = _D(near_edge), _D(deep_edge), _D(sl)
    # Point entry (near == deep): there is no distinct deep edge to rest a runner
    # LIMIT against — deploying it at the same level the toe-in already took is a
    # limit-at-market that the broker rejects (#140). Fold the runner legs into the
    # toe-in so they deploy immediately alongside it. The reclaim tail is untouched
    # (it still arms on a break beyond the point and reclaims).
    if near == deep and part[RUNNER]:
        part = {TOE_IN: sorted(part[TOE_IN] + part[RUNNER]),
                RUNNER: [], RECLAIM: part[RECLAIM]}
    role_by_tp = {i: role for role, idxs in part.items() for i in idxs}
    atrD = _D(atr) if atr is not None else None
    priceD = _D(current_price) if current_price is not None else None
    off = (_D(cfg.get("stop_offset_atr") or 0) * atrD) if atrD else _D(0)
    reclaim_trig = deep + off if direction == "BUY" else deep - off

    # --- toe-in mode, decided ONCE (it is the same for every toe-in leg) --------
    # Mirrors build_plan: the candle range folded together with the live price
    # decides "already reached" (#153); failing that, an "enter now" hint fills at
    # the live price within the chase tolerance (#151).
    highs = [x for x in (_D(candle_high) if candle_high is not None else None, priceD)
             if x is not None]
    lows = [x for x in (_D(candle_low) if candle_low is not None else None, priceD)
            if x is not None]
    crossed = _entry_crossed(direction, near,
                             max(highs) if highs else None,
                             min(lows) if lows else None)
    chased = False
    if priceD is not None and not crossed and market_hint:
        chased = (_chase_gap(direction, priceD, near)
                  <= _chase_tolerance(near, slD, atrD,
                                      _D(chase_tolerance_r), _D(chase_tolerance_atr)))
        # Beyond the tolerance with beyond_tolerance="skip" the control account
        # trades nothing at all — so neither may the staged one, or the two arms
        # stop taking the same signals (#155).
        if not chased and str(beyond_tolerance) == "skip":
            return []

    legs = []
    for i, tp in enumerate(tps or [], start=1):
        role = role_by_tp.get(i)
        if role is None:
            continue
        tpD = _D(tp)
        if role == TOE_IN:
            # A crossed/chased toe-in fills at the LIVE price, not at the candle
            # extreme that crossed it — same as build_plan's ("MARKET", current_price).
            if (crossed or chased) and priceD is not None:
                entry, mode, trigger = priceD, "MARKET", None
            else:
                entry, mode, trigger = near, "LIMIT", None
        elif role == RUNNER:
            entry, mode, trigger = deep, "LIMIT", None
        else:
            entry, mode, trigger = reclaim_trig, "STOP", reclaim_trig
        leg = PlannedLeg(side=direction, entry=entry, tp=tpD, sl=slD, tp_index=i,
                         order_type=mode, tranche=role, trigger=trigger)
        protective = entry > slD if direction == "BUY" else entry < slD
        beyond = tpD > entry if direction == "BUY" else tpD < entry
        if not protective:
            leg.valid, leg.skip_reason = False, "sl on wrong side of entry"
        elif not beyond:
            leg.valid, leg.skip_reason = False, "tp already passed at deploy level"
        elif min_stop_distance is not None and abs(tpD - entry) < _D(min_stop_distance):
            leg.valid, leg.skip_reason = False, "tp within broker min distance"
        elif (max_tp_distance_pct is not None and entry
              and abs(tpD - entry) / abs(entry) > _D(max_tp_distance_pct)):
            # A TP an implausible distance from entry (e.g. tp=1530 for gold near
            # 4180) is a parse artifact, not a real target. Same guard, same reason
            # string as build_plan — the two arms must drop the same legs (#152).
            pct = int(abs(tpD - entry) / abs(entry) * 100)
            leg.valid, leg.skip_reason = False, f"tp implausibly far ({pct}% from entry)"
        legs.append(leg)
    return legs


# ============================ context + decision ==============================
@dataclass(frozen=True)
class StagingContext:
    """The live snapshot the monitor feeds the decider each tick. Pure data —
    prices in instrument units, ATR absolute (price units) or None.

    `signals` is the extensibility hook: future modifier deciders read their
    inputs from here (e.g. signals['trend_aligned'], signals['regime']) without
    changing this dataclass or the engine signature."""
    direction: str
    near_edge: float
    deep_edge: float
    sl: float
    price: float
    atr: Optional[float] = None
    max_adverse_beyond_deep: float = 0.0
    signals: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StagingDecision:
    action: str                     # WAIT | DEPLOY | EXPIRE | SKIP
    role: str
    mode: Optional[str] = None      # order mode when action == DEPLOY
    size_factor: float = 1.0        # <=1 multiplier a modifier may shrink to
    level: Optional[float] = None   # trigger/limit level for the order
    reason: str = ""


# A modifier decider: reads (role, state, ctx, cfg) and may return a dict with
# any of {"skip": bool, "size_factor": float, "reason": str} to make a would-be
# DEPLOY more conservative. Returning None (or {}) is a no-op. It can NEVER cause
# a deploy or up-size — that invariant lives in `_apply_modifier`.
Decider = Callable[..., Optional[dict]]
DECIDERS: List[Decider] = []        # empty today; append to extend (see module doc)


def _mechanical_decision(role, state, ctx: StagingContext, cfg: dict,
                         minutes_in_state: float) -> StagingDecision:
    """The base, purely-mechanical decision from price geometry + TTLs — before
    any modifier decider runs."""
    if role == RUNNER:
        if state != PENDING:
            return StagingDecision(WAIT, role, reason=f"runner already {state}")
        if _reached(ctx.direction, ctx.price, ctx.deep_edge):
            # Price is already AT/through the deep edge, so a LIMIT resting at the
            # deep edge would sit on the wrong side of the market and the broker
            # rejects it (error.validation.limit.price, #140). Deploy MARKET — the
            # same crossed->MARKET intent the toe-in uses. A BUY fills <= deep (a
            # SELL >= deep), i.e. at or better than the planned deep-edge entry, so
            # this never increases the tranche's risk versus the resting LIMIT.
            return StagingDecision(DEPLOY, role, mode=MODE_MARKET, level=ctx.deep_edge,
                                   reason="price reached deep edge -> deploy MARKET")
        if minutes_in_state >= float(cfg.get("runner_ttl_minutes", 60)):
            return StagingDecision(EXPIRE, role, reason="runner TTL: deep edge untouched")
        return StagingDecision(WAIT, role, reason="waiting for deep edge")

    if role == RECLAIM:
        if state == PENDING:
            d = break_distance(cfg, ctx.atr, abs(ctx.deep_edge - ctx.sl))
            if d is not None and ctx.max_adverse_beyond_deep >= d:
                lvl = reclaim_stop_level(ctx.direction, ctx.deep_edge, ctx.atr, cfg)
                return StagingDecision(DEPLOY, role, mode=MODE_STOP, level=lvl,
                                       reason="broke beyond deep edge -> arm reclaim STOP")
            if minutes_in_state >= float(cfg.get("reclaim_pending_ttl_minutes", 60)):
                return StagingDecision(EXPIRE, role, reason="reclaim TTL: no break beyond deep")
            return StagingDecision(WAIT, role, reason="waiting for break beyond deep edge")
        if state == ARMED:
            if minutes_in_state >= float(cfg.get("reclaim_armed_ttl_minutes", 60)):
                return StagingDecision(EXPIRE, role, reason="reclaim TTL: armed but never reclaimed")
            return StagingDecision(WAIT, role, reason="armed; waiting for reclaim fill")
        return StagingDecision(WAIT, role, reason=f"reclaim {state}")

    return StagingDecision(WAIT, role, reason="toe-in is deployed at entry")


def _apply_modifier(base: StagingDecision, mod: Optional[dict]) -> StagingDecision:
    """Fold one modifier into the base decision, CONSERVATIVELY. Modifiers only
    bite on a DEPLOY: they may veto it (skip) or shrink its size_factor. They can
    never create a deploy, raise size, or change an EXPIRE/WAIT — so a new signal
    plugged in later cannot open or enlarge a position by accident."""
    if not mod or base.action != DEPLOY:
        return base
    reason = mod.get("reason") or ""
    if mod.get("skip"):
        return StagingDecision(SKIP, base.role, reason=reason or "vetoed by decider")
    sf = mod.get("size_factor")
    if sf is not None:
        try:
            sf = float(sf)
        except (TypeError, ValueError):
            return base
        if sf <= 0.0:
            return StagingDecision(SKIP, base.role, mode=base.mode, level=base.level,
                                   reason=reason or "decider size_factor 0")
        new_sf = min(base.size_factor, sf)          # only ever shrink
        if new_sf < base.size_factor:
            return StagingDecision(base.action, base.role, mode=base.mode,
                                   size_factor=new_sf, level=base.level,
                                   reason=(base.reason + f"; {reason}").strip("; "))
    return base


def decide_tranche(*, role: str, state: str, ctx: StagingContext, cfg: dict,
                   minutes_in_state: float = 0.0,
                   deciders: Optional[List[Decider]] = None) -> StagingDecision:
    """The DECIDE step. Compose the mechanical base decision with every modifier
    decider (conservative-only). This is what the monitor calls per pending
    tranche each tick; carrying out the returned action is the monitor's job."""
    base = _mechanical_decision(role, state, ctx, cfg, minutes_in_state)
    for d in (deciders if deciders is not None else DECIDERS):
        try:
            base = _apply_modifier(base, d(role=role, state=state, ctx=ctx, cfg=cfg))
        except Exception:
            # A misbehaving modifier must never crash the tick or, worse, open a
            # trade — swallow and keep the (already-computed) conservative base.
            continue
    return base
