"""Batch reconstruction of per-signal excursions from the candle store (#182).

The DB half of `analysis/excursion.py`: pull the bars once, replay every signal
against them, and persist one `SignalExcursion` row per (signal, basis) so the
Bayesian layer and the weekly report query a label instead of recomputing a
400k-bar replay per read.

Read-only with respect to trading: it SELECTs signals/trades/legs/candles and
writes only its own table. Nothing here can gate or alter a trade.

Two bases, because they answer different questions:
  `signal` — the channel's stated entry. Labels ALL signals, including the ones
             we skipped, filtered, risk-blocked or never filled. That is the only
             way to score what a filter REJECTED, and it takes the labelled set
             from ~519 filled trades to the full signal history.
  `fill`   — our actual fill, timed off the `events` audit trail rather than the
             trade row. The gap to the signal basis is the execution cost.
"""
from __future__ import annotations

import bisect
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select

from ..db.models import Candle, Event, Leg, Signal, SignalExcursion, Trade
from ..logging import get_logger
from .excursion import DEFAULT_HORIZON_BARS, LADDER, excursion

log = get_logger("analytics.excursion")

BASIS_SIGNAL = "signal"
BASIS_FILL = "fill"

# The candle columns the replay needs — pulled as tuples, not ORM objects, so a
# multi-hundred-thousand-bar window stays a list of floats rather than a session
# full of identity-mapped instances.
_BAR_COLS = (Candle.ts, Candle.high_bid, Candle.low_bid, Candle.high_ask, Candle.low_ask)


def _f(v) -> Optional[float]:
    return None if v is None else float(v)


def _tp1(signal) -> Optional[float]:
    """First rung of the signal's own TP ladder — the level whose distance in R
    varies 0.15 to 1.00 across channels and confounds every win rate."""
    for t in (signal.tps or []):
        try:
            return float(t)
        except (TypeError, ValueError):
            continue
    return None


async def load_bars(session, symbol: str, timeframe: str = "1m") -> tuple[list, list]:
    """(timestamps, bars) for the usable candles, ascending. `quality != 'ok'` is
    EXCLUDED — crossed quotes and outlier bars would print phantom wicks that
    falsely trigger a TP or an SL — and the caller reports how many were dropped
    (#169: flag, never auto-repair)."""
    rows = (await session.execute(
        select(*_BAR_COLS)
        .where(Candle.symbol == symbol, Candle.timeframe == timeframe,
               Candle.quality == "ok")
        .order_by(Candle.ts))).all()
    ts = [r[0] for r in rows]
    bars = [{"high_bid": _f(r[1]), "low_bid": _f(r[2]),
             "high_ask": _f(r[3]), "low_ask": _f(r[4])} for r in rows]
    return ts, bars


def _window(ts: list, bars: list, start, horizon_bars: int) -> list:
    """The bars from `start` forward. `bisect_left` on the ascending open times
    gives the first bar that opened at or after the entry — a bar already in
    progress is skipped rather than credited with a wick that predates us."""
    if start is None or not ts:
        return []
    i = bisect.bisect_left(ts, start)
    return bars[i:i + horizon_bars]


# The entry clock, in descending order of precision. Leg rows carry a fill PRICE
# but no fill TIME, so the instant comes from the `events` audit trail:
#
#   filled           the monitor saw a working order become a position — the only
#                    record of when a resting LIMIT actually got hit. Bounded by
#                    one monitor tick, which is inside the 1m bar either way.
#   staged_deployed  a staged MARKET tranche; it opens on the spot, so the deploy
#                    instant IS the fill instant (#129).
#   placed           the executor's MARKET leg; placement and fill are the same
#                    call, and legs are placed one at a time with a rate-limit
#                    sleep between them, so this beats a shared trade timestamp.
#
# Ordered most- to least-precise; the first kind present for a leg wins.
FILL_EVENT_KINDS = ("filled", "staged_deployed", "placed")
CLOCK_TRADE = "trade"          # last resort: the trade row's creation time


def pick_fill_entry(legs, events):
    """The (trade_id, entry_price, entry_at, clock_source) our money actually
    entered on, for ONE signal. Pure — the DB half is `_fill_basis`.

    `legs`: [{leg_id, trade_id, fill_price, trade_created_at}] ·
    `events`: {(leg_id, kind): ts}

    A leg with no usable fill price is skipped — a stored 0 is an UNKNOWN fill,
    not a fill at zero (#159). Of the rest we take the EARLIEST clock rather than
    the lowest leg id: a fanout's legs fill at different times, and the excursion
    window has to open when the first of them put money on the table."""
    best = None
    for leg in legs or []:
        price = leg.get("fill_price")
        if not price:
            continue
        at, source = None, CLOCK_TRADE
        for kind in FILL_EVENT_KINDS:
            ts = events.get((leg.get("leg_id"), kind))
            if ts is not None:
                at, source = ts, kind
                break
        if at is None:
            at = leg.get("trade_created_at")
        if at is None:
            continue
        key = (at, leg.get("leg_id") or 0)
        if best is None or key < best[0]:
            best = (key, (leg.get("trade_id"), float(price), at, source))
    return None if best is None else best[1]


async def _fill_basis(session, signal_ids: list) -> dict:
    """{signal_id: (trade_id, entry_price, entry_at, clock_source)} for the
    actually-filled trades, timed off the `events` audit trail (see
    FILL_EVENT_KINDS).

    This matters most where it used to be worst: a resting LIMIT entry can fill
    hours after the trade row was created, and replaying from trade creation
    credited the signal with every tick that printed while the order was still
    unfilled — inventing excursion we were never in the market for."""
    if not signal_ids:
        return {}
    rows = (await session.execute(
        select(Trade.signal_id, Trade.id, Trade.created_at, Leg.fill_price, Leg.id)
        .join(Leg, Leg.trade_id == Trade.id)
        .where(Trade.signal_id.in_(signal_ids))
        .order_by(Trade.signal_id, Trade.id, Leg.id))).all()
    if not rows:
        return {}

    leg_ids = [r[4] for r in rows]
    events: dict = {}
    for leg_id, kind, ts in (await session.execute(
            select(Event.leg_id, Event.kind, Event.ts)
            .where(Event.leg_id.in_(leg_ids),
                   Event.kind.in_(FILL_EVENT_KINDS))
            .order_by(Event.ts))).all():
        events.setdefault((leg_id, kind), ts)     # earliest wins (ordered by ts)

    by_signal: dict = {}
    for sid, tid, created, fill, leg_id in rows:
        by_signal.setdefault(sid, []).append(
            {"leg_id": leg_id, "trade_id": tid, "fill_price": fill,
             "trade_created_at": created})
    out: dict = {}
    for sid, legs in by_signal.items():
        picked = pick_fill_entry(legs, events)
        if picked is not None:
            out[sid] = picked
    return out


async def _suspect_count(session, symbol: str, timeframe: str) -> int:
    return int((await session.execute(
        select(func.count()).select_from(Candle)
        .where(Candle.symbol == symbol, Candle.timeframe == timeframe,
               Candle.quality != "ok"))).scalar() or 0)


async def _sl_truth(session, signal_ids: list) -> dict:
    """{signal_id: True} where a leg of that signal's trade actually recorded an
    `sl_hit`. The validation gate: a reconstruction that disagrees with what the
    broker did is the reconstruction being wrong, not the data."""
    if not signal_ids:
        return {}
    rows = (await session.execute(
        select(Trade.signal_id, Leg.outcome)
        .join(Leg, Leg.trade_id == Trade.id)
        .where(Trade.signal_id.in_(signal_ids)))).all()
    out: dict = {}
    for sid, outcome in rows:
        if outcome == "sl_hit":
            out[sid] = True
    return out


async def recompute(session, *, symbol: str = "XAUUSD", timeframe: str = "1m",
                    horizon_bars: int = DEFAULT_HORIZON_BARS,
                    ladder=LADDER, frm=None, to=None) -> dict:
    """Replay every signal against the candles and upsert its excursion rows.

    Returns the run summary the acceptance criteria ask to be REPORTED, not
    buried: how many resolved vs hit the horizon cap, how many were same-bar
    ambiguous, how many candles were excluded as suspect, the coverage window,
    and the SL agreement rate against broker-recorded leg outcomes."""
    ts, bars = await load_bars(session, symbol, timeframe)
    suspect = await _suspect_count(session, symbol, timeframe)
    coverage = {"n_bars": len(ts),
                "first": ts[0].isoformat() if ts else None,
                "last": ts[-1].isoformat() if ts else None,
                "suspect_excluded": suspect}
    if not ts:
        return {"ok": False, "reason": "no usable candles for %s %s" % (symbol, timeframe),
                "coverage": coverage, "written": 0}

    q = select(Signal).where(Signal.symbol == symbol)
    if frm is not None:
        q = q.where(Signal.created_at >= frm)
    if to is not None:
        q = q.where(Signal.created_at < to)
    signals = (await session.execute(q.order_by(Signal.id))).scalars().all()
    sids = [s.id for s in signals]
    fills = await _fill_basis(session, sids)
    sl_truth = await _sl_truth(session, sids)

    existing = {(r.signal_id, r.basis): r for r in (await session.execute(
        select(SignalExcursion).where(SignalExcursion.signal_id.in_(sids)))
    ).scalars().all()} if sids else {}

    counts = {"signals": len(signals), "no_coverage": 0, "no_geometry": 0,
              "resolved": 0, "horizon_capped": 0, "same_bar_ambiguous": 0,
              "written": 0, "fill_basis": 0}
    # Where each fill-basis clock came from — the mix is the honest statement of
    # how precisely the fill window is timed (see FILL_EVENT_KINDS).
    clocks = {k: 0 for k in FILL_EVENT_KINDS + (CLOCK_TRADE,)}
    agree = {"n": 0, "agreed": 0}

    for s in signals:
        entry_sig = _f(s.entry_from)
        sl = _f(s.sl)
        if entry_sig is None or sl is None:
            counts["no_geometry"] += 1
            continue
        tp1 = _tp1(s)

        for basis in (BASIS_SIGNAL, BASIS_FILL):
            if basis == BASIS_SIGNAL:
                entry, start, trade_id = entry_sig, s.created_at, None
                clock = BASIS_SIGNAL
            else:
                fb = fills.get(s.id)
                if fb is None:
                    continue
                trade_id, entry, start, clock = fb
            win = _window(ts, bars, start, horizon_bars)
            if not win:
                if basis == BASIS_SIGNAL:
                    counts["no_coverage"] += 1
                continue
            res = excursion(win, direction=s.direction, entry=entry, sl=sl,
                            tp1=tp1, ladder=ladder, horizon_bars=horizon_bars)
            if res is None:
                counts["no_geometry"] += 1
                continue

            if basis == BASIS_SIGNAL:
                counts["resolved" if res["resolved"] else "horizon_capped"] += 1
                if res["same_bar_ambiguous"]:
                    counts["same_bar_ambiguous"] += 1
                if sl_truth.get(s.id):
                    agree["n"] += 1
                    agree["agreed"] += 1 if res["race"] == "sl" else 0
            else:
                counts["fill_basis"] += 1
                clocks[clock] = clocks.get(clock, 0) + 1

            _upsert(session, existing, s, basis, trade_id, entry, sl, tp1, res,
                    horizon_bars, start, clock)
            counts["written"] += 1

    await session.commit()
    counts["agreement_sl"] = {
        "n": agree["n"],
        "rate": round(agree["agreed"] / agree["n"], 4) if agree["n"] else None,
        "note": ("Share of broker-recorded sl_hit trades whose reconstruction also "
                 "raced to the stop. Materially below 1.0 means the reconstruction "
                 "is wrong, not the data — do not act on the label until it holds."),
    }
    counts["fill_clock"] = {
        **clocks,
        "note": ("Where each fill-basis excursion window started. 'filled' is the "
                 "monitor seeing a working order become a position — the exact "
                 "instant a resting LIMIT was hit. 'trade' is the fallback: no fill "
                 "event on any leg, so the window opens at trade creation and may "
                 "include travel from before we were in. A large 'trade' share is a "
                 "caveat on the fill basis; the signal basis is unaffected."),
    }
    return {"ok": True, "symbol": symbol, "timeframe": timeframe,
            "horizon_bars": horizon_bars, "ladder": list(ladder),
            "coverage": coverage, **counts}


def _upsert(session, existing: dict, signal, basis: str, trade_id, entry: float,
            sl: float, tp1, res: dict, horizon_bars: int,
            entry_at=None, clock_source: str = None) -> None:
    """Write (or refresh) one row. Recomputing must be idempotent — a wider
    horizon or a corrected candle range should update in place, not accumulate a
    second answer for the same signal."""
    vals = dict(
        trade_id=trade_id, symbol=signal.symbol, direction=signal.direction,
        entry=Decimal(str(entry)), sl=Decimal(str(sl)),
        tp1=None if tp1 is None else Decimal(str(tp1)),
        r=Decimal(str(res["r"])),
        tp1_r=None if res["tp1_r"] is None else Decimal(str(round(res["tp1_r"], 6))),
        mfe_r=Decimal(str(round(res["mfe_r"], 6))),
        mae_r=Decimal(str(round(res["mae_r"], 6))),
        race=res["race"], bars_to_tp1=res["bars_to_tp1"], bars_to_sl=res["bars_to_sl"],
        ladder=res["ladder"], same_bar_ambiguous=res["same_bar_ambiguous"],
        horizon_capped=res["horizon_capped"], horizon_bars=horizon_bars,
        n_bars=res["n_bars"], entry_at=entry_at, clock_source=clock_source)
    row = existing.get((signal.id, basis))
    if row is None:
        row = SignalExcursion(signal_id=signal.id, basis=basis, **vals)
        session.add(row)
        existing[(signal.id, basis)] = row
    else:
        for k, v in vals.items():
            setattr(row, k, v)


async def labels_by_signal(session, *, basis: str = BASIS_SIGNAL,
                           frm=None, to=None) -> dict:
    """{signal_id: {mfe_r, mae_r, tp1_r, race, …}} for the Bayesian layer. Reads
    the persisted rows — point-in-time, never a fresh replay on a request path."""
    q = select(SignalExcursion).where(SignalExcursion.basis == basis)
    if frm is not None or to is not None:
        q = q.join(Signal, Signal.id == SignalExcursion.signal_id)
        if frm is not None:
            q = q.where(Signal.created_at >= frm)
        if to is not None:
            q = q.where(Signal.created_at < to)
    return {r.signal_id: {"mfe_r": _f(r.mfe_r), "mae_r": _f(r.mae_r),
                          "tp1_r": _f(r.tp1_r), "race": r.race,
                          "horizon_capped": bool(r.horizon_capped)}
            for r in (await session.execute(q)).scalars().all()}
