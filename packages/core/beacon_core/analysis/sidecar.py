"""Shadow analytics sidecar — foundation (#52).

The isolation harness + persistence for the analytics estimators (#51). Runs
each estimator best-effort at signal-capture time (already off the execution
hot path — capture fires in the background AFTER orders are placed) and writes
one SignalAnalytics row per signal. HARD RULE: pure observability — an estimator
that errors or returns nothing is swallowed and logged (`ANALYTICS-SIDECAR-
DEGRADED`); it can never block, delay, or alter a trade.

Phase-1 estimators register themselves in ESTIMATORS (see estimators.py, #53);
this module carries none of them so the plumbing ships and is testable alone.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Dict, List, Optional

from ..logging import get_logger

# NOTE: sqlalchemy / db-model / settings imports are deferred into the functions
# that need them (capture_analytics, load_config) so the estimator harness
# (run_estimators / build_window / AnalyticsCtx) imports with only stdlib and is
# unit-testable without the DB stack installed.

log = get_logger("analytics.sidecar")

ANALYTICS_SETTING_KEY = "analytics"
DEFAULT_ANALYTICS = {
    "enabled": True,        # pure observability + off the hot path -> on by default
    "timeframe": "1h",      # primary price-window timeframe for series estimators
    "window_bars": 200,     # max closes retained for reproducibility
}

# Estimator registry: name -> callable(ctx) -> JSON-able output (or None to skip).
# Callables may be sync or async (async ones can query history, e.g. k-NN).
# Populated by estimators.py so the harness stays dependency-free (#53).
Estimator = Callable[["AnalyticsCtx"], object]
ESTIMATORS: "Dict[str, Estimator]" = {}


def register_estimator(name: str, fn: Estimator) -> None:
    ESTIMATORS[name] = fn


def _register_builtin() -> None:
    """Register the Phase-1 estimators (#53). Kept out of estimators.py so that
    module imports only stdlib (dev-testable); sidecar depends on estimators,
    never the reverse."""
    try:
        from . import estimators
        for _name, _fn in estimators.ESTIMATORS.items():
            ESTIMATORS.setdefault(_name, _fn)
    except Exception as exc:                         # never break capture on a bad estimator import
        log.warning("ANALYTICS-SIDECAR-DEGRADED: estimator registration failed: %s", exc)


_register_builtin()


@dataclass
class AnalyticsCtx:
    """Everything an estimator may need, assembled once per signal. `session` is
    provided for estimators that query history (k-NN); series estimators use the
    price window; regime/VWAP estimators read the already-computed `features`."""
    signal_id: int
    symbol: str
    direction: Optional[str]
    price: Optional[float]
    timeframe: str
    closes: List[float] = field(default_factory=list)
    highs: List[float] = field(default_factory=list)
    lows: List[float] = field(default_factory=list)
    volumes: List[float] = field(default_factory=list)
    features: dict = field(default_factory=dict)     # multi-timeframe TA snapshot
    session: object = None                           # AsyncSession (optional)
    source_id: Optional[int] = None


async def load_config(session) -> dict:
    from ..settings_store import get_setting
    from ._util import overlay_config
    return overlay_config(DEFAULT_ANALYTICS, await get_setting(session, ANALYTICS_SETTING_KEY, None))


def build_window(ctx: AnalyticsCtx, max_bars: int) -> dict:
    """A compact, reproducible snapshot of the price window an estimator saw."""
    closes = [round(float(c), 5) for c in ctx.closes[-max_bars:]]
    return {"timeframe": ctx.timeframe, "n": len(closes), "closes": closes,
            "price": round(float(ctx.price), 5) if ctx.price is not None else None}


async def run_estimators(ctx: AnalyticsCtx, estimators=None):
    """Run every estimator in isolation. Returns (analytics, degraded). A failure
    in one estimator never affects the others (or the trade) — it is swallowed
    and its name recorded in `degraded`."""
    estimators = ESTIMATORS if estimators is None else estimators
    analytics: dict = {}
    degraded: List[str] = []
    for name, fn in estimators.items():
        try:
            res = fn(ctx)
            if inspect.isawaitable(res):
                res = await res
            if res is not None:
                analytics[name] = res
        except Exception as exc:                     # ISOLATION: never propagates
            degraded.append(name)
            log.warning("ANALYTICS-SIDECAR-DEGRADED: estimator '%s' failed "
                        "(signal %s): %s", name, ctx.signal_id, exc)
    # #70: realize the locked envelope — one uniform contributions view across
    # every estimator, via the single central mapper. Per-estimator detail dicts
    # stay untouched (back-compat); this is an additive block consumers may adopt.
    from .contract import estimator_contributions
    contribs = [c for n, out in analytics.items()
                for c in estimator_contributions(n, out)]
    if contribs:
        analytics["_contributions"] = contribs
    return analytics, degraded


async def capture_analytics(*, signal_id: int, symbol: str, direction,
                            source_id, features: dict, bars: list, price,
                            timeframe: str, cfg: dict = None) -> Optional[dict]:
    """Build the ctx, run the estimator suite, and upsert one SignalAnalytics row
    in its OWN session/transaction — fully isolated from TA capture, so an
    estimator's DB read (e.g. k-NN) can never poison the capture transaction or
    the trade. Best-effort: the caller also wraps this so any failure is swallowed.
    Takes primitives (not ORM objects) so nothing is bound to another session."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from ..db.base import Session
    from ..db.models import SignalAnalytics
    from ._util import bars_col

    async with Session()() as session:
        cfg = cfg if cfg is not None else await load_config(session)
        if not cfg.get("enabled"):
            return None
        ctx = AnalyticsCtx(
            signal_id=signal_id, symbol=symbol, direction=direction,
            price=float(price) if price is not None else None,
            timeframe=timeframe, closes=bars_col(bars, "c"), highs=bars_col(bars, "h"),
            lows=bars_col(bars, "l"),
            volumes=bars_col(bars, "v"), features=features or {}, session=session,
            source_id=source_id,
        )
        analytics, degraded = await run_estimators(ctx)
        regime = None
        _r = analytics.get("regime")
        if isinstance(_r, dict):
            regime = _r.get("label")

        stmt = pg_insert(SignalAnalytics).values(
            signal_id=signal_id, symbol=symbol, direction=direction,
            regime=regime, price=Decimal(str(price)) if price is not None else None,
            window=build_window(ctx, int(cfg.get("window_bars", 200))),
            analytics=analytics, degraded=degraded,
        ).on_conflict_do_nothing(constraint="uq_signal_analytics")
        await session.execute(stmt)
        await session.commit()
        log.info("analytics sidecar: signal %s (%s estimators, %s degraded)",
                 signal_id, len(analytics), len(degraded))
        return analytics
