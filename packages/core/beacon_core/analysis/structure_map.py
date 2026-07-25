"""Persistence + versioned recompute for the market-structure/magnet map (#61).

Layer A is slow-moving: recompute writes a NEW version per symbol and supersedes
the prior (point-in-time correctness). The per-signal Layer-B reference reads the
active map via `active_map()` (it does NOT recompute). Shadow-only — nothing here
touches the execution path; recompute runs weekly (config) / on demand.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from ..logging import get_logger
from ..timeutil import utcnow
from ..ta.indicators import atr as _atr
from . import structure as S
from ._util import bars_col

log = get_logger("analytics.structure")

ANALYTICS_STRUCTURE_KEY = "structure"
_ATR_PERIOD = 14


async def load_config(session) -> dict:
    from ..settings_store import get_setting
    return S.structure_cfg(await get_setting(session, ANALYTICS_STRUCTURE_KEY, None))


def _bar_cols(bars):
    return bars_col(bars, "h"), bars_col(bars, "l"), bars_col(bars, "c")


async def recompute_symbol(session, adapter, symbol: str, broker_epic: str,
                           cfg: dict) -> Optional[int]:
    """Recompute the full multi-TF map for one symbol and persist it as a new
    version, superseding the prior. Returns the new version_id (or None if no
    timeframe produced usable structure). Caller commits."""
    from sqlalchemy import select, delete
    from ..db.models import MarketStructure, StructureLevel, MagnetZone

    now = utcnow()
    tfs = cfg.get("timeframes") or S.DEFAULT_STRUCTURE["timeframes"]
    retr = cfg.get("fib_retracement") or []
    ext = cfg.get("fib_extension") or []
    kzt = cfg.get("zigzag_k_by_tf") or {}
    tf_w = cfg.get("tf_weights") or {}
    kind_w = cfg.get("kind_weights") or {}
    min_bars = cfg.get("min_bars_by_tf") or {}
    max_bars = int(cfg.get("max_bars", 300))
    emit_fvg = bool(cfg.get("emit_fvg", True))
    fvg_min_gap = float(cfg.get("fvg_min_gap_atr", 0.25))
    fvg_lookback = int(cfg.get("fvg_lookback", 50))

    prev_ver = (await session.execute(select(MarketStructure.version_id).where(
        MarketStructure.symbol == symbol, MarketStructure.active == True)
        .limit(1))).scalar_one_or_none()
    version_id = (prev_ver or 0) + 1

    struct_rows = []          # (tf, MarketStructure, [level dicts])
    fvg_by_tf = {}            # tf -> [fvg level dicts] (own kind, NOT in all-kind zones)
    all_levels = []           # flat list for clustering (post-flush level_id filled)
    atr_1h = None

    for tf in tfs:
        resolution = S.STRUCT_TF_RESOLUTION.get(tf)
        if not resolution:
            continue
        try:
            bars = await adapter.get_bars(broker_epic, resolution, max_bars=max_bars)
        except Exception as exc:
            log.info("structure: bars %s/%s failed: %s", symbol, tf, exc)
            continue
        highs, lows, closes = _bar_cols(bars)
        if len(closes) < int(min_bars.get(tf, 40)):
            continue
        a = _atr(highs, lows, closes, _ATR_PERIOD)
        if a is None or a <= 0:
            continue
        if tf == "1h":
            atr_1h = a
        res = S.analyze_timeframe(bars, atr=a, k=float(kzt.get(tf, 1.5)),
                                  retr_ratios=retr, ext_ratios=ext)
        if res is None:
            continue
        if emit_fvg:
            fvg_by_tf[tf] = S.find_fvgs(highs, lows, a, min_gap_atr=fvg_min_gap,
                                        lookback=fvg_lookback)
        ms = MarketStructure(
            symbol=symbol, timeframe=tf, version_id=version_id, label=res["label"],
            swings=res["swings"],
            bias_price=Decimal(str(res["bias_price"])) if res["bias_price"] is not None else None,
            premium_discount=(Decimal(str(round(res["premium_discount"], 6)))
                              if res["premium_discount"] is not None else None),
            # Persist the prem/disc reference range so premium_discount is
            # reproducible from the stored row (#113), not just from the last 8 swings.
            range_low=(Decimal(str(round(res["range_low"], 6)))
                       if res.get("range_low") is not None else None),
            range_high=(Decimal(str(round(res["range_high"], 6)))
                        if res.get("range_high") is not None else None),
            atr=Decimal(str(round(a, 6))), active=True, computed_at=now)
        session.add(ms)
        struct_rows.append((tf, ms, res["levels"]))

    if not struct_rows:
        return None

    # Latest-only retention (#137): DELETE the prior generation for this symbol in
    # the SAME transaction rather than keeping superseded history (11 versions was
    # ~91% dead structure_levels rows). Child tables first (structure_levels FKs
    # market_structure). Per-signal snapshots (signal_analytics.structure_magnet)
    # are the historical record, so pruning the map is safe — accepted consequence:
    # a past signal's map_version_id may point at a pruned version. Schema/columns
    # are unchanged (versioning stays); only superseded rows are removed.
    for _model in (MagnetZone, StructureLevel, MarketStructure):
        await session.execute(delete(_model).where(
            _model.symbol == symbol, _model.version_id != version_id))

    await session.flush()     # get MarketStructure ids

    # Level rows (one per level), weighted by tf_weight * kind_weight. FVG levels
    # (#137) are persisted as their own `kind` for the per-kind panel but are held
    # OUT of `all_levels` so the all-kind magnet zones (which the per-signal
    # estimator reads) stay byte-identical to before this change.
    for tf, ms, levels in struct_rows:
        for lv in levels:
            w = float(tf_w.get(tf, 1.0)) * float(kind_w.get(lv["kind"], 1.0))
            row = StructureLevel(
                symbol=symbol, timeframe=tf, version_id=version_id, structure_id=ms.id,
                kind=lv["kind"],
                ratio=Decimal(str(lv["ratio"])) if lv.get("ratio") is not None else None,
                price=Decimal(str(round(lv["price"], 6))),
                anchor_a=lv.get("anchor_a"), anchor_b=lv.get("anchor_b"),
                anchor_c=lv.get("anchor_c"),
                direction=lv.get("direction"), weight=Decimal(str(round(w, 6))),
                active=True, computed_at=now)
            session.add(row)
            all_levels.append({"row": row, "price": float(lv["price"]), "weight": w,
                               "timeframe": tf, "kind": lv["kind"], "ratio": lv.get("ratio")})
        # FVG levels for this TF — stored, but excluded from the all-kind clustering.
        for g in fvg_by_tf.get(tf, []):
            w = float(tf_w.get(tf, 1.0)) * float(kind_w.get("fvg", 0.8))
            session.add(StructureLevel(
                symbol=symbol, timeframe=tf, version_id=version_id, structure_id=ms.id,
                kind="fvg", ratio=None, price=Decimal(str(round(g["price"], 6))),
                # band + fill state ride in anchor_a so the panel can show Open/Filled.
                anchor_a={"top": round(g["top"], 6), "bottom": round(g["bottom"], 6),
                          "filled": bool(g["filled"]), "direction": g["direction"]},
                direction=g.get("direction"), weight=Decimal(str(round(w, 6))),
                active=True, computed_at=now))

    await session.flush()     # get StructureLevel ids

    # Cluster into magnet zones. Tolerance = cluster_atr * ATR(1h) (fallback: any tf ATR);
    # width capped at max_zone_width_atr * ATR(1h) so a chain can't fuse a whole range
    # into one "mega-zone" (#113).
    ref_atr = atr_1h or float(struct_rows[0][1].atr)
    tol = float(cfg.get("cluster_atr", 0.5)) * ref_atr
    max_w = float(cfg.get("max_zone_width_atr", 1.0)) * ref_atr
    cluster_input = [{"level_id": x["row"].id, "price": x["price"], "weight": x["weight"],
                      "timeframe": x["timeframe"], "kind": x["kind"], "ratio": x["ratio"]}
                     for x in all_levels]
    zones = S.cluster_levels(cluster_input, tolerance=tol, max_width=max_w)
    for z in zones:
        session.add(MagnetZone(
            symbol=symbol, version_id=version_id,
            price_low=Decimal(str(round(z["price_low"], 6))),
            price_high=Decimal(str(round(z["price_high"], 6))),
            mid=Decimal(str(round(z["mid"], 6))), score=Decimal(str(z["score"])),
            rank=z["rank"], n_timeframes=z["n_timeframes"],
            ref_atr=Decimal(str(round(ref_atr, 6))), members=z["members"],
            active=True, computed_at=now))

    log.info("structure: recomputed %s v%s — %s TFs, %s levels, %s zones",
             symbol, version_id, len(struct_rows), len(all_levels), len(zones))
    return version_id


async def recompute_all(cfg: dict = None) -> dict:
    """Driver: resolve an adapter + epic per configured symbol and recompute each.
    Opens its own session (fully isolated from trading). Returns {symbol: version}."""
    from ..db.base import Session
    from ..db.models import Account
    from sqlalchemy import select
    from ..brokers import build_adapter, symbol_map

    out = {}
    async with Session()() as session:
        cfg = cfg or await load_config(session)
        if not cfg.get("enabled"):
            return out
        acct = (await session.execute(select(Account).where(
            Account.enabled == True).limit(1))).scalar_one_or_none()
        if acct is None:
            log.info("structure: no enabled account to source bars from; skipping")
            return out
        broker, adapter = await build_adapter(session, acct)
        try:
            for symbol in (cfg.get("symbols") or []):
                smap = await symbol_map(session, broker.id, symbol)
                if not smap:
                    log.info("structure: no symbol map for %s on broker %s", symbol, broker.id)
                    continue
                try:
                    ver = await recompute_symbol(session, adapter, symbol,
                                                 smap.broker_epic, cfg)
                    if ver is not None:
                        out[symbol] = ver
                except Exception as exc:
                    log.warning("structure: recompute %s failed: %s", symbol, exc)
                    await session.rollback()
            await session.commit()
        finally:
            try:
                await adapter.aclose()
            except Exception:
                pass
    return out


async def active_map(session, symbol: str) -> Optional[dict]:
    """The active (current) Layer-A map for a symbol: version + per-TF structure +
    that TF's levels + the magnet zones. Read-only; used by the per-signal
    estimator. None when no map has been computed yet."""
    from sqlalchemy import select
    from ..db.models import MarketStructure, StructureLevel, MagnetZone

    structs = (await session.execute(select(MarketStructure).where(
        MarketStructure.symbol == symbol, MarketStructure.active == True))).scalars().all()
    if not structs:
        return None
    version_id = structs[0].version_id
    levels = (await session.execute(select(StructureLevel).where(
        StructureLevel.symbol == symbol, StructureLevel.active == True))).scalars().all()
    zones = (await session.execute(select(MagnetZone).where(
        MagnetZone.symbol == symbol, MagnetZone.active == True)
        .order_by(MagnetZone.rank))).scalars().all()
    levels_by_tf = {}
    for lv in levels:
        levels_by_tf.setdefault(lv.timeframe, []).append(lv)
    return {"version_id": version_id,
            "structures": {s.timeframe: s for s in structs},
            "levels_by_tf": levels_by_tf, "zones": zones}


# Panel timeframe order (#137) — fine -> coarse, matching the frontend selector.
PANEL_TFS = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]


async def kind_zones(session, symbol: str, kind: str, price: Optional[float] = None,
                     cfg: dict = None) -> Optional[dict]:
    """Per-kind, side-aware confluence zones for the panel (#137). Clusters the
    ACTIVE structure levels of `kind` at query time (kind-agnostic: `fvg` today,
    `order_block`/fib/swing reuse the same path) into buy-side (below price) and
    sell-side (above price) nearest-3, plus a per-timeframe breakdown. Read-only;
    does NOT touch the all-kind magnet zones or the trading path. None when no map."""
    from sqlalchemy import select
    from ..db.models import MarketStructure, StructureLevel

    cfg = cfg or await load_config(session)
    structs = (await session.execute(select(MarketStructure).where(
        MarketStructure.symbol == symbol, MarketStructure.active == True))).scalars().all()
    if not structs:
        return None
    version_id = structs[0].version_id
    per_tf_atr = {s.timeframe: float(s.atr) for s in structs if s.atr is not None}
    ref_atr = per_tf_atr.get("1h") or next(iter(per_tf_atr.values()), 0.0)

    ref_price = price
    if ref_price is None:                       # freshest-TF close captured at recompute
        for tf in ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"):
            s = next((x for x in structs if x.timeframe == tf), None)
            if s is not None and s.bias_price is not None:
                ref_price = float(s.bias_price)
                break

    rows = (await session.execute(select(StructureLevel).where(
        StructureLevel.symbol == symbol, StructureLevel.active == True,
        StructureLevel.kind == kind))).scalars().all()
    levels = []
    for lv in rows:
        d = {"level_id": lv.id, "price": float(lv.price), "weight": float(lv.weight or 0),
             "timeframe": lv.timeframe, "kind": lv.kind,
             "ratio": float(lv.ratio) if lv.ratio is not None else None}
        if isinstance(lv.anchor_a, dict) and "filled" in lv.anchor_a:
            d["filled"] = bool(lv.anchor_a.get("filled"))
        levels.append(d)

    present_tfs = [t for t in PANEL_TFS if any(l["timeframe"] == t for l in levels)]
    if ref_price is None or not levels:
        sides = {"buy_side": [], "sell_side": [], "per_tf": {}}
    else:
        sides = S.side_aware_kind_zones(levels, float(ref_price), ref_atr=ref_atr,
                                        cfg=cfg, per_tf_atr=per_tf_atr, timeframes=PANEL_TFS)
    return {"symbol": symbol, "kind": kind, "version_id": version_id,
            "reference_price": ref_price, "ref_atr": ref_atr,
            "buy_side": sides["buy_side"], "sell_side": sides["sell_side"],
            "per_tf": sides["per_tf"], "timeframes": present_tfs}
