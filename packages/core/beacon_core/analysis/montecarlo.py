"""Monte Carlo option pricing + the signal-geometry null model.

PORT — the pricing core below is a line-for-line Python port of the reference
C++ in `PyPatel/Options-Trading-Strategies-in-Python :: Monte Carlo Option
Pricing/`:

  * `mcm.cpp` :: `GetOneGaussianBySummation` / `GetOneGaussianByBoxMuller`
  * `Monte_Carlo_call_option_pricer_simple.cpp` :: `SimpleMonteCarlo1`

Same variable names, same order of operations, same Ito correction, same
discounting, so the two can be diffed side by side. Two deliberate deviations,
both noted at the call site: (a) `rand()/RAND_MAX` becomes a seeded
`random.Random` so a persisted per-signal number is REPRODUCIBLE, and (b) the
Box-Muller rejection loop also rejects `sizeSquared == 0`, which would be a
ZeroDivisionError in Python where C++ silently yields inf.

EXTENSION — `barrier_outcomes` drives that SAME GBM process, discretised into
steps, to answer the question Beacon actually needs: given a signal's
entry/SL/TP geometry and the volatility measured at signal time, what is
P(TP1 before SL) **if the channel has no skill at all**? That is the null a
channel's realized win-rate has to beat. A channel posting SL at 3xATR and TP1
at 0.3xATR wins ~90% of the time by arithmetic alone; without this number a
90% win-rate reads as edge.

WHAT THE NULL DOES AND DOES NOT SAY. Under a driftless price process expected R
is ZERO for every geometry — wide-stop/near-target and tight-stop/far-target
both break even, they just trade win-rate against payoff ratio. So `expected_r`
is a CALIBRATION DIAGNOSTIC (it should come out ~0; if it doesn't, the
simulation is mis-specified), not a ranking field. The informative outputs are
`p_win_geometry` — the win-rate to expect with no skill — and, against it,
the channel's realized win-rate. Costs are not modelled: in reality the same
geometry loses by the spread, so a channel merely MATCHING its null is losing.

SHADOW / measure-before-gate: computed, persisted and logged beside live
trading. Nothing here gates, resizes or delays a trade.
"""
from __future__ import annotations

import math
import random
from statistics import pstdev
from typing import Callable, List, Optional

# Bars per year per timeframe, on a ~6000-hour gold trading year (23h x 5d x 52w).
# Only ever used to split a per-bar sigma into (annual vol, expiry-in-years); the
# convention CANCELS in vol^2 * expiry, so no result depends on this table — it
# exists so `vol_annual` and `expiry_years` are readable in the persisted row.
BARS_PER_YEAR = {"1m": 360000.0, "5m": 72000.0, "15m": 24000.0, "30m": 12000.0,
                 "1h": 6000.0, "4h": 1500.0, "1d": 250.0}

DEFAULT_MONTECARLO = {
    "enabled": True,
    "paths": 10000,        # barrier paths per signal
    "steps": 24,           # GBM steps to the horizon (one per bar by default)
    "horizon_bars": 24,    # how far ahead the null looks, in analytics-TF bars
    "r": 0.0,              # risk-free drift. 0.0 = the driftless null (see below)
    "price_paths": 20000,  # paths for the reference European-option pricer
    "bridge": True,        # Brownian-bridge correction for discrete monitoring
}


# ============================ reference port ==================================
def GetOneGaussianBySummation(rng: random.Random) -> float:
    """`mcm.cpp` :: GetOneGaussianBySummation — sum 12 uniforms, subtract 6.

    Kept for fidelity with the reference (it is the other generator the original
    ships); `GetOneGaussianByBoxMuller` is what the pricer actually calls."""
    result = 0.0
    for _j in range(12):
        result += rng.random()
    result -= 6.0
    return result


def GetOneGaussianByBoxMuller(rng: random.Random) -> float:
    """`mcm.cpp` :: GetOneGaussianByBoxMuller — Marsaglia polar method.

    Draws (x, y) uniformly in the square [-1,1]^2, rejects outside the unit
    circle, and returns one normal variate. Like the original it DISCARDS the
    second variate (`y * sqrt(...)`) rather than caching it — that is a real
    inefficiency in the reference, preserved on purpose."""
    while True:
        x = 2.0 * rng.random() - 1
        y = 2.0 * rng.random() - 1
        size_squared = x * x + y * y
        if size_squared < 1.0 and size_squared > 0.0:     # `> 0` is the Python guard
            break
    return x * math.sqrt(-2 * math.log(size_squared) / size_squared)


def SimpleMonteCarlo1(Expiry: float, Strike: float, Spot: float, Vol: float,
                      r: float, NumberOfPaths: int,
                      rng: random.Random = None,
                      gaussian: Callable[[random.Random], float] = None) -> float:
    """`Monte_Carlo_call_option_pricer_simple.cpp` :: SimpleMonteCarlo1.

    European CALL by risk-neutral simulation, verbatim:
        varience      = Vol * Vol * Expiry
        rootVarience  = sqrt(varience)
        itoCorrection = -0.5 * varience
        movedSpot     = Spot * exp(r*Expiry + itoCorrection)
        thisSpot      = movedSpot * exp(rootVarience * thisGaussian)
        thisPayoff    = max(thisSpot - Strike, 0)
        mean          = (runningSum / NumberOfPaths) * exp(-r * Expiry)
    """
    rng = rng or random.Random()
    gaussian = gaussian or GetOneGaussianByBoxMuller
    varience = Vol * Vol * Expiry
    rootVarience = math.sqrt(varience)
    itoCorrection = -0.5 * varience

    movedSpot = Spot * math.exp(r * Expiry + itoCorrection)
    runningSum = 0.0
    for _i in range(NumberOfPaths):
        thisGaussian = gaussian(rng)
        thisSpot = movedSpot * math.exp(rootVarience * thisGaussian)
        thisPayoff = thisSpot - Strike
        thisPayoff = thisPayoff if thisPayoff > 0 else 0.0
        runningSum += thisPayoff
    mean = runningSum / NumberOfPaths
    mean *= math.exp(-r * Expiry)
    return mean


def SimpleMonteCarloPut(Expiry: float, Strike: float, Spot: float, Vol: float,
                        r: float, NumberOfPaths: int,
                        rng: random.Random = None,
                        gaussian: Callable[[random.Random], float] = None) -> float:
    """European PUT — the reference's own declared next step ("Upcoming changes:
    Give Price Puts"). Identical to SimpleMonteCarlo1 with the payoff flipped to
    max(Strike - thisSpot, 0); everything else is unchanged."""
    rng = rng or random.Random()
    gaussian = gaussian or GetOneGaussianByBoxMuller
    varience = Vol * Vol * Expiry
    rootVarience = math.sqrt(varience)
    itoCorrection = -0.5 * varience

    movedSpot = Spot * math.exp(r * Expiry + itoCorrection)
    runningSum = 0.0
    for _i in range(NumberOfPaths):
        thisGaussian = gaussian(rng)
        thisSpot = movedSpot * math.exp(rootVarience * thisGaussian)
        thisPayoff = Strike - thisSpot
        thisPayoff = thisPayoff if thisPayoff > 0 else 0.0
        runningSum += thisPayoff
    mean = runningSum / NumberOfPaths
    mean *= math.exp(-r * Expiry)
    return mean


# ============================ Beacon extension ================================
def log_return_vol(closes: List[float]) -> Optional[float]:
    """Per-bar LOG-return standard deviation (decimal, not percent).

    Deliberately not `estimators.realized_vol` (simple returns, in percent): the
    GBM above lives in log space, so its sigma must be a log-return sigma. Kept
    local so this module imports nothing from estimators (which imports it)."""
    cs = [float(c) for c in closes if c is not None and float(c) > 0]
    if len(cs) < 3:
        return None
    rets = [math.log(cs[i] / cs[i - 1]) for i in range(1, len(cs))]
    if len(rets) < 2:
        return None
    return pstdev(rets)


def _bridge_hit(prev: float, cur: float, barrier: float, var_step: float,
                upper: bool, rng: random.Random) -> bool:
    """Did the path touch `barrier` between two sampled points?

    True whenever an endpoint is already at/through the barrier; otherwise draws
    against the Brownian-bridge hitting probability

        P = exp(-2 * ln(H/S_prev) * ln(H/S_cur) / (vol^2 * dt))

    which is EXACT for the same log-Brownian process the steps are drawn from —
    it corrects the discrete-monitoring bias without changing the process. At
    steps=24 the uncorrected sampler under-detects touches by ~7pp on a tight-TP
    geometry (measured), which would flatter every wide-stop channel."""
    if var_step <= 0 or prev <= 0 or cur <= 0 or barrier <= 0:
        return False
    if upper:
        if prev >= barrier or cur >= barrier:
            return True
        p = math.exp(-2.0 * math.log(barrier / prev) * math.log(barrier / cur) / var_step)
    else:
        if prev <= barrier or cur <= barrier:
            return True
        p = math.exp(-2.0 * math.log(prev / barrier) * math.log(cur / barrier) / var_step)
    return rng.random() < p


def barrier_outcomes(*, spot: float, sl: float, tps: List[float], direction: str,
                     vol: float, expiry: float, r: float = 0.0,
                     paths: int = 10000, steps: int = 24, bridge: bool = True,
                     rng: random.Random = None,
                     gaussian: Callable[[random.Random], float] = None) -> Optional[dict]:
    """First-touch race between TP1 and SL under the SAME GBM as SimpleMonteCarlo1.

    The single-step `movedSpot * exp(rootVarience * Z)` of the reference is the
    exact solution of dS = rS dt + vol S dW at T; here that process is stepped
    `steps` times so each path can be tested against the barriers on the way.
    Per step: S *= exp((r - 0.5 vol^2) dt + vol sqrt(dt) Z) — the same drift and
    Ito correction, just at dt = expiry/steps.

    `r = 0.0` is the intended null: DRIFTLESS. A non-zero r bakes in a directional
    view, which is exactly the skill the null is supposed to withhold.

    Conventions, both chosen so the null cannot flatter a channel:
      * SL is tested BEFORE TP within a step, so a bar that straddles both counts
        as a stop-out.
      * `bridge=True` applies the exact Brownian-bridge touch probability inside
        each step (see `_bridge_hit`), so P(hit) is not the LOWER bound plain
        discrete monitoring would give. The TP LADDER depth is still measured on
        step endpoints only — deep-ladder rates stay mildly conservative.

    Returns None when the geometry is unusable (no SL distance, no TPs, vol<=0).
    """
    if not tps or vol is None or vol <= 0 or expiry <= 0 or steps < 1 or paths < 1:
        return None
    try:
        spot, sl = float(spot), float(sl)
    except (TypeError, ValueError):
        return None
    risk = abs(spot - sl)
    if risk <= 0:
        return None
    long_side = (direction or "").upper() == "BUY"
    # A signal whose SL sits the wrong side of entry is malformed, not a null.
    if long_side and sl >= spot:
        return None
    if not long_side and sl <= spot:
        return None
    ladder = [float(t) for t in tps if t is not None]
    if not ladder:
        return None

    rng = rng or random.Random()
    gaussian = gaussian or GetOneGaussianByBoxMuller
    dt = expiry / steps
    drift = (r - 0.5 * vol * vol) * dt
    diffusion = vol * math.sqrt(dt)

    tp1 = ladder[0]
    n_sl_first = n_tp1_first = 0
    reached = [0] * len(ladder)          # per-TP: paths reaching it before any SL
    r_sum = 0.0

    var_step = diffusion * diffusion     # vol^2 * dt, for the bridge correction

    def _touch(prev, cur, barrier, upper):
        if bridge:
            return _bridge_hit(prev, cur, barrier, var_step, upper, rng)
        return (cur >= barrier) if upper else (cur <= barrier)

    for _p in range(paths):
        s = spot
        outcome = None                   # "sl" | "tp1" | None (horizon)
        deepest = 0
        for _step in range(steps):
            prev = s
            s *= math.exp(drift + diffusion * gaussian(rng))
            if _touch(prev, s, sl, not long_side):
                if outcome is None:      # pessimistic: SL wins a straddled step
                    outcome = "sl"
                break                    # stopped out -> ladder frozen at `deepest`
            # How deep into the ladder this path ran WITHOUT stopping out. TP1 is
            # ladder[0], so the first-touch race result falls out of the same
            # draws — no second, independently-sampled TP1 test to disagree with.
            while deepest < len(ladder) and _touch(prev, s, ladder[deepest], long_side):
                deepest += 1
            if deepest >= 1 and outcome is None:
                outcome = "tp1"          # TP1 beat SL; keep walking for the ladder
        for i in range(deepest):
            reached[i] += 1
        if outcome == "sl":
            n_sl_first += 1
            r_sum -= 1.0
        elif outcome == "tp1":
            n_tp1_first += 1
            r_sum += abs(tp1 - spot) / risk
        else:                            # neither barrier -> mark to market
            r_sum += ((s - spot) if long_side else (spot - s)) / risk

    n = float(paths)
    p_tp1 = n_tp1_first / n
    p_sl = n_sl_first / n
    rr = abs(tp1 - spot) / risk
    p_neither = 1.0 - p_tp1 - p_sl
    # Closed-form twin of p_tp1_first: the win-rate this payoff ratio must clear to
    # break even. When essentially every path resolves, a driftless process makes
    # the two EQUAL, so `null_gap` is a sharp check that the simulation is sound.
    #
    # That identity needs the race to actually finish. When a material share of
    # paths reach neither barrier inside the horizon (`horizon_truncated`), the
    # unresolved mass sits in the mark-to-market term instead and the two are
    # legitimately different — so `null_gap` is withheld rather than reported as a
    # false alarm. `expected_r` ~ 0 remains the calibration check in every case: it
    # accounts for the unresolved paths, so it holds regardless of truncation.
    breakeven = 1.0 / (1.0 + rr) if rr > 0 else None
    truncated = p_neither > 0.02
    return {
        "paths": paths, "steps": steps, "r": r,
        "p_tp1_first": round(p_tp1, 4),
        "p_sl_first": round(p_sl, 4),
        "p_neither": round(p_neither, 4),
        # True => the horizon is short for this geometry, so p_tp1_first UNDERSTATES
        # the eventual first-touch rate. Widen `horizon_bars` before reading an edge.
        "horizon_truncated": truncated,
        "expected_r": round(r_sum / n, 4),
        "breakeven_win_rate": round(breakeven, 4) if breakeven is not None else None,
        "null_gap": (round(p_tp1 - breakeven, 4)
                     if breakeven is not None and not truncated else None),
        "rr_to_tp1": round(rr, 4),
        "tp_ladder": [{"index": i + 1, "price": round(t, 5),
                       "p_reached_before_sl": round(reached[i] / n, 4)}
                      for i, t in enumerate(ladder)],
    }


def signal_montecarlo(*, entry, sl, tps, direction, closes, timeframe: str,
                      cfg: dict = None, seed: int = 0) -> Optional[dict]:
    """The full per-signal Monte Carlo block: the reference option prices plus the
    geometry null. Pure — takes primitives, returns a JSON-able dict (or None).

    `p_win_geometry` is the headline: the probability this trade wins on geometry
    ALONE. Compare a channel's realized win-rate against it — the difference, not
    the raw win-rate, is the channel's edge.
    """
    cfg = {**DEFAULT_MONTECARLO, **(cfg or {})}
    try:
        spot = float(entry)
    except (TypeError, ValueError):
        return None
    vol_bar = log_return_vol(closes)
    if not vol_bar or spot <= 0:
        return None

    bpy = BARS_PER_YEAR.get(timeframe, BARS_PER_YEAR["1h"])
    horizon = max(1, int(cfg["horizon_bars"]))
    expiry = horizon / bpy
    vol_annual = vol_bar * math.sqrt(bpy)
    r = float(cfg.get("r", 0.0))

    # Reproducible per signal: same signal -> same persisted number, always.
    # Separate streams for pricing and barriers, so retuning `price_paths` cannot
    # silently shift `p_win_geometry` by consuming a different number of draws.
    rng = random.Random(seed)
    barrier_rng = random.Random(seed + 1)
    out = {
        "method": "gbm_box_muller",
        "source": "PyPatel/Options-Trading-Strategies-in-Python :: SimpleMonteCarlo1",
        "timeframe": timeframe, "seed": seed,
        "spot": round(spot, 5),
        "vol_bar": round(vol_bar, 6), "vol_annual": round(vol_annual, 5),
        "horizon_bars": horizon, "expiry_years": round(expiry, 8),
    }

    ladder = [float(t) for t in (tps or []) if t is not None]
    if ladder:
        # The reference pricer, applied verbatim to this signal's own geometry:
        # spot = entry, strike = TP1. A valuation, not the decision input.
        n_price = max(1, int(cfg["price_paths"]))
        out["reference_call"] = {
            "strike": round(ladder[0], 5),
            "price": round(SimpleMonteCarlo1(expiry, ladder[0], spot, vol_annual,
                                             r, n_price, rng), 5),
            "paths": n_price}
        out["reference_put"] = {
            "strike": round(ladder[0], 5),
            "price": round(SimpleMonteCarloPut(expiry, ladder[0], spot, vol_annual,
                                               r, n_price, rng), 5),
            "paths": n_price}

    bar = barrier_outcomes(spot=spot, sl=sl, tps=ladder, direction=direction,
                           vol=vol_annual, expiry=expiry, r=r,
                           paths=max(1, int(cfg["paths"])),
                           steps=max(1, int(cfg["steps"])),
                           bridge=bool(cfg.get("bridge", True)), rng=barrier_rng)
    if bar:
        out.update(bar)
        out["p_win_geometry"] = bar["p_tp1_first"]
    return out


def montecarlo_estimator(ctx) -> Optional[dict]:
    """SHADOW / measure-before-gate. Per-signal geometry null (see module doc).

    Reads the signal's own entry level (falling back to the live price) plus the
    parsed SL/TP ladder and the analytics-timeframe price window. Seeded from the
    signal id so the persisted row is reproducible."""
    conf = getattr(ctx, "config", None)
    cfg = conf.get("montecarlo") if isinstance(conf, dict) else None
    if isinstance(cfg, dict) and not cfg.get("enabled", True):
        return None
    entry = ctx.entry_from if ctx.entry_from is not None else ctx.price
    return signal_montecarlo(entry=entry, sl=ctx.sl, tps=ctx.tps,
                             direction=ctx.direction, closes=ctx.closes,
                             timeframe=ctx.timeframe, cfg=cfg,
                             seed=int(ctx.signal_id or 0))
