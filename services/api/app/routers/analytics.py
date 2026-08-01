"""Shadow analytics sidecar API (#53): the signal↔channel↔regime correlation
report and per-signal analytics. Read-only observability — nothing here gates
or alters trading.

#175 removed the routes whose only consumer was a Details tab that could not
inform a decision: `/correlation` and `/trend-alignment` (folded on features with
no variance — `regime` is 'trending' on every captured row per #111, and 4h-EMA200
alignment is a perfect relabelling of `direction`), `/structure`,
`/structure/outcome` and `/magnets` (charts that never produced an actionable
finding), and `/structure/recompute` (the manual button on the deleted Structure
card — the monitor calls `recompute_all` in-process on its own schedule, so the
map the surviving summary strip reads keeps refreshing).

CAPTURE IS UNTOUCHED: every estimator still writes `signal_analytics`, and the
report functions behind the deleted routes are still in `analysis/report.py` for
the weekly quant work. This deleted views, not evidence."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.analysis.report import (channel_verdict_report,
                                         execution_geometry_ab_report,
                                         shadow_strategy_report,
                                         turtle_exit_report)
from beacon_core.analysis import sidecar
from beacon_core.analysis.sidecar import load_config
from beacon_core.analysis import structure_map as struct_map
from beacon_core.analysis._util import nearest_sides
from beacon_core.analysis.structure import DEFAULT_STRUCTURE
from beacon_core.db.models import SignalAnalytics
from beacon_core.settings_store import set_setting
from beacon_core.timeutil import parse_iso_utc
from ..deps import get_db
from ..auth import require_token

router = APIRouter(prefix="/analytics", tags=["analytics"],
                   dependencies=[Depends(require_token)])


@router.get("/config")
async def get_config(db: AsyncSession = Depends(get_db)):
    return await load_config(db)


@router.put("/config")
async def put_config(body: dict, db: AsyncSession = Depends(get_db)):
    cfg = await load_config(db)
    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
    if body.get("timeframe"):
        cfg["timeframe"] = str(body["timeframe"])
    if "window_bars" in body:
        try:
            cfg["window_bars"] = max(30, min(500, int(body["window_bars"])))
        except (TypeError, ValueError):
            pass
    if "disabled" in body:
        # Per-estimator off switch (#168). Only names that exist can be listed:
        # a typo would otherwise read as "disabled nothing" and look identical to
        # a working switch-off. Unknown names are a 422, not a silent no-op.
        raw = body["disabled"] or []
        if isinstance(raw, str):
            raw = raw.split(",")
        if not isinstance(raw, list):
            raise HTTPException(422, "disabled must be a list of estimator names")
        names = [str(x).strip() for x in raw if str(x).strip()]
        unknown = sorted(set(names) - set(sidecar.ESTIMATORS))
        if unknown:
            raise HTTPException(422, f"unknown estimator(s): {', '.join(unknown)} "
                                     f"(known: {', '.join(sorted(sidecar.ESTIMATORS))})")
        cfg["disabled"] = names
    await set_setting(db, "analytics", cfg)
    return cfg


@router.get("/synthesis")
async def synthesis(date_from: str = None, date_to: str = None,
                    db: AsyncSession = Depends(get_db)):
    """Decision-layer synthesis (#117): the weekly per-channel keep/watch/cut
    verdict with an explicit significance state, and an honest 'no credible edge
    yet' when nothing has crossed the N floor. A pure reduction of the same
    labelled analytics→trade join `/correlation` details — no new estimator,
    nothing gates on it. Optional date range anchored on signal time."""
    return await channel_verdict_report(db, parse_iso_utc(date_from), parse_iso_utc(date_to))


@router.get("/execution-geometry")
async def execution_geometry(date_from: str = None, date_to: str = None,
                             source_id: int = None, control_account_id: int = None,
                             db: AsyncSession = Depends(get_db)):
    """Payoff-geometry A/B in R-multiples (#80/#85): per-arm (account) avg R,
    payoff ratio, profit factor, breakeven-leg rate and %-winners-reaching-≥TP3,
    with win-rate credible intervals. R = realized_pl / planned_risk is scale-free,
    so it compares arms trading different nominal sizes (equity-parity confound).
    Optional date range (anchored on signal time) and per-channel `source_id`
    scope. Shadow / read-only — judge only at N≥30 closed per arm.

    Carries the #188 `delever` block: an arm that simply risks less posts a
    better R with no selection skill, so every non-control arm is tested against
    a de-lever null and reported as NO_SKILL_DEMONSTRATED when its observed dR
    falls inside it. `control_account_id` names the control arm; it defaults to
    the lowest account id present, which is Arm A by convention."""
    return await execution_geometry_ab_report(
        db, parse_iso_utc(date_from), parse_iso_utc(date_to), source_id=source_id,
        control_account_id=control_account_id)


@router.get("/shadow-strategies")
async def shadow_strategies(date_from: str = None, date_to: str = None,
                            db: AsyncSession = Depends(get_db)):
    """Monte Carlo geometry null + Turtle breakout vs realized outcome.

    The headline is `montecarlo.edge`: realized win-rate MINUS the win-rate each
    signal's own SL/TP geometry implies with no channel skill assumed. A channel
    posting a far stop and a near target wins most of its trades by arithmetic —
    only the excess over its own null is evidence. `turtle` splits the same
    trades by whether the 55-bar Donchian system agreed with the channel.
    Shadow / read-only — neither gates."""
    return await shadow_strategy_report(db, parse_iso_utc(date_from), parse_iso_utc(date_to))


@router.get("/turtle-exit")
async def turtle_exit(date_from: str = None, date_to: str = None,
                      symbol: str = "XAUUSD", timeframe: str = "1h",
                      window: int = 55, variant: str = "signal",
                      db: AsyncSession = Depends(get_db)):
    """Turtle exit counterfactual: would closing on a trend flip have beaten
    where each trade actually closed?

    Replays the 55-bar Donchian across every closed trade's holding period and
    prices the exit a flip would have forced. Costs ONE ranged bar fetch for the
    whole report (every trade is the same instrument), not one per trade.

    `mean_delta_r` is the decision number and must clear zero by more than
    `stderr_delta_r` at N>=30 before a Turtle exit is worth wiring into the live
    SL engine. Read `flip_rate` beside it — a rule that rarely fires cannot help
    much — and check the result is not just an artifact of stop distance: a
    55-bar flip is SLOW, so it can only beat a stop that sits far away.

    Shadow / read-only. Nothing here moves a stop or closes a position."""
    from beacon_core.brokers import build_adapter, symbol_map
    from beacon_core.db.models import Account

    acct = (await db.execute(select(Account).where(
        Account.enabled == True).limit(1))).scalar_one_or_none()   # noqa: E712
    if acct is None:
        raise HTTPException(503, "no enabled account to source bars from")
    broker, adapter = await build_adapter(db, acct)
    try:
        smap = await symbol_map(db, broker.id, symbol)
        if not smap:
            raise HTTPException(404, f"no symbol map for {symbol} on broker {broker.id}")
        return await turtle_exit_report(
            db, adapter, smap.broker_epic, symbol=symbol,
            frm=parse_iso_utc(date_from), to=parse_iso_utc(date_to),
            timeframe=timeframe, window=max(2, int(window)), variant=variant)
    finally:
        try:
            await adapter.aclose()
        except Exception:
            pass


@router.get("/structure/config")
async def structure_config(db: AsyncSession = Depends(get_db)):
    return await struct_map.load_config(db)


@router.put("/structure/config")
async def structure_config_put(body: dict, db: AsyncSession = Depends(get_db)):
    cfg = await struct_map.load_config(db)
    for k in DEFAULT_STRUCTURE:
        if k in body:
            cfg[k] = body[k]
    await set_setting(db, "structure", cfg)
    return cfg


@router.get("/structure/map")
async def structure_map_view(symbol: str = "XAUUSD", price: float = None,
                             db: AsyncSession = Depends(get_db)):
    """The active (current) structure/magnet map: per-TF structure + magnet zones.

    Also surfaces the nearest magnet on EACH side of a reference price (#116) so a
    consumer never has to infer resistance-vs-support from the score-ranked `zones`
    list (which the higher-scoring side otherwise dominates). `price` defaults to
    the finest-TF close captured at recompute time when not supplied."""
    m = await struct_map.active_map(db, symbol)
    if not m:
        return {"symbol": symbol, "version_id": None, "structures": {}, "zones": [],
                "reference_price": None, "nearest_resistance": None, "nearest_support": None}

    ref_price = price
    if ref_price is None:                       # fall back to the freshest recompute-time close
        for tf in ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"):
            s = m["structures"].get(tf)
            if s is not None and s.bias_price is not None:
                ref_price = float(s.bias_price)
                break

    zones = m["zones"]

    def _side_zone(i):
        if i is None:
            return None
        z = zones[i]
        lo, hi = float(z.price_low), float(z.price_high)
        ref_atr = float(z.ref_atr) if z.ref_atr else None
        d = (lo - ref_price) if lo > ref_price else (ref_price - hi)
        return {"rank": z.rank, "band": [lo, hi], "mid": float(z.mid),
                "score": float(z.score), "n_timeframes": z.n_timeframes,
                "dist": round(d, 5), "dist_atr": round(d / ref_atr, 3) if ref_atr else None}

    res_i = sup_i = None
    if ref_price is not None:
        res_i, sup_i = nearest_sides([(float(z.price_low), float(z.price_high)) for z in zones], ref_price)

    return {
        "symbol": symbol, "version_id": m["version_id"],
        "structures": {tf: {
            "label": s.label,
            "premium_discount": float(s.premium_discount) if s.premium_discount is not None else None,
            "atr": float(s.atr) if s.atr is not None else None,
            "swings": s.swings, "n_levels": len(m["levels_by_tf"].get(tf, [])),
        } for tf, s in m["structures"].items()},
        "zones": [{
            "rank": z.rank, "band": [float(z.price_low), float(z.price_high)],
            "mid": float(z.mid), "score": float(z.score),
            "n_timeframes": z.n_timeframes, "members": z.members,
        } for z in m["zones"]],
        "reference_price": ref_price,
        "nearest_resistance": _side_zone(res_i),
        "nearest_support": _side_zone(sup_i),
    }


@router.get("/signal/{signal_id}")
async def signal_analytics(signal_id: int, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(SignalAnalytics).where(
        SignalAnalytics.signal_id == signal_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "no analytics for this signal")
    return {"signal_id": row.signal_id, "symbol": row.symbol,
            "direction": row.direction, "regime": row.regime,
            "analytics": row.analytics, "degraded": row.degraded,
            "window": row.window,
            "captured_at": row.captured_at.isoformat() if row.captured_at else None}
