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

log = get_logger("analytics.structure")

ANALYTICS_STRUCTURE_KEY = "structure"
_ATR_PERIOD = 14


async def load_config(session) -> dict:
    from ..settings_store import get_setting
    return S.structure_cfg(await get_setting(session, ANALYTICS_STRUCTURE_KEY, None))


def _bar_cols(bars):
    highs = [float(b["h"]) for b in bars if b.get("h") is not None]
    lows = [float(b["l"]) for b in bars if b.get("l") is not None]
    closes = [float(b["c"]) for b in bars if b.get("c") is not None]
    return highs, lows, closes


async def recompute_symbol(session, adapter, symbol: str, broker_epic: str,
                           cfg: dict) -> Optional[int]:
    """Recompute the full multi-TF map for one symbol and persist it as a new
    version, superseding the prior. Returns the new version_id (or None if no
    timeframe produced usable structure). Caller commits."""
    from sqlalchemy import select, update
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

    prev_ver = (await session.execute(select(MarketStructure.version_id).where(
        MarketStructure.symbol == symbol, MarketStructure.active == True)
        .limit(1))).scalar_one_or_none()
    version_id = (prev_ver or 0) + 1

    struct_rows = []          # (tf, MarketStructure, [level dicts])
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
        ms = MarketStructure(
            symbol=symbol, timeframe=tf, version_id=version_id, label=res["label"],
            swings=res["swings"],
            bias_price=Decimal(str(res["bias_price"])) if res["bias_price"] is not None else None,
            premium_discount=(Decimal(str(round(res["premium_discount"], 6)))
                              if res["premium_discount"] is not None else None),
            atr=Decimal(str(round(a, 6))), active=True, computed_at=now)
        session.add(ms)
        struct_rows.append((tf, ms, res["levels"]))

    if not struct_rows:
        return None

    # Supersede the prior active generation (all three tables) for this symbol.
    for _model in (MarketStructure, StructureLevel, MagnetZone):
        await session.execute(update(_model).where(
            _model.symbol == symbol, _model.active == True,
            _model.version_id != version_id
        ).values(active=False, superseded_at=now))

    await session.flush()     # get MarketStructure ids

    # Level rows (one per level), weighted by tf_weight * kind_weight.
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

    await session.flush()     # get StructureLevel ids

    # Cluster into magnet zones. Tolerance = cluster_atr * ATR(1h) (fallback: any tf ATR).
    ref_atr = atr_1h or float(struct_rows[0][1].atr)
    tol = float(cfg.get("cluster_atr", 0.5)) * ref_atr
    cluster_input = [{"level_id": x["row"].id, "price": x["price"], "weight": x["weight"],
                      "timeframe": x["timeframe"], "kind": x["kind"], "ratio": x["ratio"]}
                     for x in all_levels]
    zones = S.cluster_levels(cluster_input, tolerance=tol)
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
