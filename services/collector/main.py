"""Collector service — Alpha Layer Phase 0 (data foundation).

Captures top-of-book ticks per active SymbolMap on a fixed cadence, derives 1m
OHLC candles (backfilled from the broker on boot), snapshots crypto
microstructure, refreshes the economic calendar, and rebuilds spread cost
profiles nightly. Everything is GMT/UTC and Decimal.

Fail-safe: any single source failing logs and is skipped; the loop never dies.
Broker sessions are reused (one cached adapter per broker) per the platform
guardrail — no per-call logins.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from beacon_core.bus import Bus
from beacon_core.config import get_settings
from beacon_core.logging import get_logger
from beacon_core.health import run_health_server
from beacon_core.db.base import Session, init_models
from beacon_core.db.models import (Broker, Candle, CostProfile, CryptoMicro,
                                   EconEvent, SymbolMap, Tick)
from beacon_core.brokers import get_adapter, resolve_credentials
from beacon_core.brokers.types import AuthError
from beacon_core.marketsessions import session_for
from beacon_core.instruments import binance_symbol
from beacon_core.alpha import calendar as econ_calendar
from beacon_core.alpha import crypto_micro as cmicro

log = get_logger("collector")
settings = get_settings()
bus = Bus()

# Reused broker sessions (guardrail: single cached CST/token, re-auth on 401).
_ADAPTERS: dict = {}
# In-memory forming 1m candle per symbol: {symbol: {minute,o,h,l,c,session}}.
_CANDLE: dict = {}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_bar_ts(v) -> Optional[dt.datetime]:
    if not v:
        return None
    try:
        d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except (ValueError, AttributeError):
        return None


async def _adapter_for(session, broker_id: int):
    adapter = _ADAPTERS.get(broker_id)
    if adapter is None:
        broker = await session.get(Broker, broker_id)
        if not broker or not broker.enabled:
            return None
        creds = resolve_credentials(broker.credentials_ref)
        creds.setdefault("is_demo", broker.is_demo)
        try:
            adapter = get_adapter(broker.type, creds)
        except Exception as exc:
            log.warning("adapter build failed (broker %s): %s", broker_id, exc)
            return None
        _ADAPTERS[broker_id] = adapter
    return adapter


async def _evict(broker_id: int) -> None:
    a = _ADAPTERS.pop(broker_id, None)
    if a is not None:
        try:
            await a.aclose()
        except Exception:
            pass


async def _symbols(session):
    return (await session.execute(select(SymbolMap))).scalars().all()


# ---- candle derivation ----------------------------------------------------
def _roll_candle(symbol: str, ts: dt.datetime, mid: Decimal) -> Optional[dict]:
    """Fold a mid price into the forming 1m candle; return the completed prior
    candle when the minute rolls over, else None."""
    minute = ts.replace(second=0, microsecond=0)
    st = _CANDLE.get(symbol)
    if st is not None and st["minute"] == minute:
        st["h"] = max(st["h"], mid)
        st["l"] = min(st["l"], mid)
        st["c"] = mid
        return None
    done = None
    if st is not None:
        done = {"symbol": symbol, "ts": st["minute"], "o": st["o"], "h": st["h"],
                "l": st["l"], "c": st["c"], "session": st["session"]}
    _CANDLE[symbol] = {"minute": minute, "o": mid, "h": mid, "l": mid, "c": mid,
                       "session": session_for(minute)}
    return done


async def _write_candles(session, rows: list) -> None:
    if not rows:
        return
    stmt = pg_insert(Candle).values([
        {"symbol": r["symbol"], "resolution": "1m", "ts": r["ts"], "o": r["o"],
         "h": r["h"], "l": r["l"], "c": r["c"], "volume": r.get("volume"),
         "session": r["session"]} for r in rows
    ]).on_conflict_do_nothing(constraint="uq_candle")
    await session.execute(stmt)


# ---- tick capture ---------------------------------------------------------
async def _capture(session) -> None:
    now = _utcnow()
    sess = session_for(now)
    completed = []
    for sm in await _symbols(session):
        adapter = await _adapter_for(session, sm.broker_id)
        if adapter is None:
            continue
        try:
            q = await adapter.get_quote(sm.broker_epic)
        except AuthError:
            await _evict(sm.broker_id)
            continue
        except Exception as exc:
            log.info("quote failed %s: %s", sm.broker_epic, exc)
            continue
        if q.bid is None or q.offer is None:
            continue
        bid, offer = q.bid, q.offer
        mid = (bid + offer) / Decimal(2)
        session.add(Tick(symbol=sm.internal_symbol, ts=now, bid=bid, offer=offer,
                         spread=offer - bid, mid=mid, session=sess))
        done = _roll_candle(sm.internal_symbol, now, mid)
        if done is not None:
            completed.append(done)
    await _write_candles(session, completed)
    await session.commit()


async def _backfill(session) -> None:
    """Seed deeper 1m history from the broker on boot."""
    for sm in await _symbols(session):
        adapter = await _adapter_for(session, sm.broker_id)
        if adapter is None:
            continue
        try:
            bars = await adapter.get_bars(sm.broker_epic, "MINUTE",
                                          max_bars=settings.candle_backfill_bars)
        except Exception as exc:
            log.info("backfill bars failed %s: %s", sm.broker_epic, exc)
            continue
        rows = []
        for b in bars:
            ts = _parse_bar_ts(b.get("t"))
            if ts is None or b.get("o") is None or b.get("c") is None:
                continue
            o = Decimal(str(b["o"]))
            rows.append({
                "symbol": sm.internal_symbol, "ts": ts, "o": o,
                "h": Decimal(str(b["h"])) if b.get("h") is not None else o,
                "l": Decimal(str(b["l"])) if b.get("l") is not None else o,
                "c": Decimal(str(b["c"])),
                "volume": Decimal(str(b["v"])) if b.get("v") is not None else None,
                "session": session_for(ts),
            })
        await _write_candles(session, rows)
        log.info("backfill %s: %s bars", sm.internal_symbol, len(rows))
    await session.commit()


# ---- crypto microstructure ------------------------------------------------
async def _crypto(session) -> None:
    now = _utcnow()
    for sm in await _symbols(session):
        bsym = binance_symbol(sm.internal_symbol)
        if not bsym:
            continue
        micro = await cmicro.fetch_micro(bsym)
        if micro is None:
            continue
        recent = (await session.execute(select(Candle).where(
            Candle.symbol == sm.internal_symbol, Candle.resolution == "1m")
            .order_by(Candle.ts.desc()).limit(30))).scalars().all()
        cds = [{"h": c.h, "l": c.l, "c": c.c} for c in reversed(recent)]
        liq = cmicro.liquidation_proxy(cds) if cds else False
        session.add(CryptoMicro(
            symbol=sm.internal_symbol, ts=now, funding=micro["funding"],
            funding_predicted=micro["funding_predicted"], basis=micro["basis"],
            ob_imbalance=micro["ob_imbalance"], liquidation_proxy=liq))
    await session.commit()


# ---- economic calendar ----------------------------------------------------
async def _calendar(session) -> None:
    events = await econ_calendar.fetch_events()
    if not events:
        return
    rows = [{"ts": e["ts"], "ccy": e.get("ccy"), "impact": e.get("impact"),
             "title": (e.get("title") or "")[:256]} for e in events if e.get("ts")]
    if rows:
        stmt = pg_insert(EconEvent).values(rows).on_conflict_do_nothing(constraint="uq_econ_event")
        await session.execute(stmt)
        await session.commit()
    log.info("calendar: %s events", len(rows))


# ---- cost profiles (nightly) ----------------------------------------------
_COST_SQL = text("""
  SELECT symbol, session,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY spread) AS median,
         percentile_cont(0.9) WITHIN GROUP (ORDER BY spread) AS p90,
         stddev_pop(spread) AS vol,
         count(*) AS n
  FROM ticks WHERE ts >= :since
  GROUP BY symbol, session
""")


async def _cost_profiles(session) -> None:
    since = _utcnow() - dt.timedelta(days=14)
    res = (await session.execute(_COST_SQL, {"since": since})).all()
    for symbol, sess, median, p90, vol, n in res:
        vals = {
            "median_spread": Decimal(str(median)) if median is not None else None,
            "p90_spread": Decimal(str(p90)) if p90 is not None else None,
            "spread_vol": Decimal(str(vol)) if vol is not None else None,
            "samples": int(n or 0), "updated_at": _utcnow(),
        }
        stmt = pg_insert(CostProfile).values(symbol=symbol, session=sess, **vals)
        stmt = stmt.on_conflict_do_update(constraint="uq_cost_profile", set_=vals)
        await session.execute(stmt)
    await session.commit()
    log.info("cost_profiles: %s symbol/session rows", len(res))


async def main() -> None:
    await init_models()
    asyncio.create_task(run_health_server("collector", bus, port=8080))
    log.info("collector: tick every %ss", settings.collect_interval)

    async with Session()() as s:
        try:
            await _backfill(s)
        except Exception as exc:
            log.warning("boot backfill failed: %s", exc)

    # last-run monotonic stamps; 0.0 forces each sub-job on the first tick.
    last = {"crypto": 0.0, "calendar": 0.0, "cost": 0.0}
    while True:
        t0 = time.monotonic()
        try:
            async with Session()() as s:
                await _capture(s)
                if t0 - last["crypto"] >= settings.crypto_micro_interval:
                    last["crypto"] = t0
                    try:
                        await _crypto(s)
                    except Exception as exc:
                        log.warning("crypto micro failed: %s", exc)
                if t0 - last["calendar"] >= settings.calendar_refresh_interval:
                    last["calendar"] = t0
                    try:
                        await _calendar(s)
                    except Exception as exc:
                        log.warning("calendar refresh failed: %s", exc)
                if t0 - last["cost"] >= settings.cost_profile_interval:
                    last["cost"] = t0
                    try:
                        await _cost_profiles(s)
                    except Exception as exc:
                        log.warning("cost profiles failed: %s", exc)
        except Exception as exc:
            log.exception("collector tick failed: %s", exc)
        await asyncio.sleep(max(0.5, settings.collect_interval - (time.monotonic() - t0)))


if __name__ == "__main__":
    asyncio.run(main())
