"""Turtle-style Donchian breakout — a mechanical second opinion on every signal.

PORT — `reference_signals` is a line-for-line Python port of
`PyPatel/Options-Trading-Strategies-in-Python :: Turtle Trading.py`, including
its NaN semantics. The original is pandas over daily bars:

    stock['high'] = stock.Close.shift(1).rolling(window=55).max()
    stock['low']  = stock.Close.shift(1).rolling(window=55).min()
    stock['avg']  = stock.Close.shift(1).rolling(window=55).mean()
    stock['long_entry']  = stock.Close > stock.high
    stock['short_entry'] = stock.Close < stock.low
    stock['long_exit']   = stock.Close < stock.avg
    stock['short_exit']  = stock.Close > stock.avg
    positions_long  = NaN, 1 at long_entry,  0 at long_exit
    positions_short = NaN, -1 at short_entry, 0 at short_exit
    Signal = positions_long + positions_short      # NaN + x = NaN
    stock  = stock.fillna(method='ffill')
    returns = log(Close/Close.shift(1)) * Signal.shift(1)

KNOWN QUIRK, REPRODUCED ON PURPOSE — `Signal` is summed BEFORE the ffill, so it
is non-NaN only on bars where BOTH position columns are non-NaN. A breakout
above the 55-bar high is necessarily above the 55-bar mean, so `long_entry`
implies `short_exit` (and vice versa) and those bars do produce a value; a plain
exit bar does not, and forward-fills the previous state instead. The net effect
is that the reference never goes flat — it is a stop-and-reverse system, not the
long/flat/short one its comments describe.

That is faithful to the source, but a shadow meant to VALIDATE signals should
not quietly inherit a bug, so `stateful_signals` also computes the documented
intent (exit to flat). Both are persisted per signal: `signal` is the reference
value, `signal_flat` the intended one. Where they disagree, the reference is
holding a position the comments say should have been closed.

SHADOW / measure-before-gate: computed, persisted and logged beside live
trading. Nothing here gates, resizes or delays a trade.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import List, Optional

DEFAULT_TURTLE = {
    "enabled": True,
    "window": 55,          # the reference's 55-bar Donchian channel
}


def _rolling_shifted(closes: List[float], window: int, fn):
    """`Close.shift(1).rolling(window=w).<fn>()` — at bar i the window covers
    closes[i-w : i], i.e. the w bars BEFORE i, excluding i itself. None (NaN)
    until enough history exists, exactly like pandas."""
    out: List[Optional[float]] = [None] * len(closes)
    for i in range(len(closes)):
        lo = i - window
        if lo < 0:
            continue
        out[i] = fn(closes[lo:i])
    return out


def _gt(a, b) -> bool:
    """`a > b` with NaN semantics: any comparison against NaN is False."""
    return a is not None and b is not None and a > b


def _lt(a, b) -> bool:
    return a is not None and b is not None and a < b


def channel(closes: List[float], window: int = 55) -> Optional[dict]:
    """The 55-bar Donchian high/low/mean at the LAST bar, on shifted closes."""
    if not closes or len(closes) <= window:
        return None
    prior = closes[-window - 1:-1]
    return {"high": max(prior), "low": min(prior), "avg": sum(prior) / len(prior)}


def reference_signals(closes: List[float], window: int = 55) -> dict:
    """The reference script's own series, verbatim (see module doc for the quirk).

    Returns every intermediate column so the port can be diffed against pandas:
    high/low/avg, the four boolean triggers, positions_long/short, and the
    forward-filled `signal`."""
    n = len(closes)
    high = _rolling_shifted(closes, window, max)
    low = _rolling_shifted(closes, window, min)
    avg = _rolling_shifted(closes, window, lambda w: sum(w) / len(w))

    long_entry = [_gt(closes[i], high[i]) for i in range(n)]
    short_entry = [_lt(closes[i], low[i]) for i in range(n)]
    long_exit = [_lt(closes[i], avg[i]) for i in range(n)]
    short_exit = [_gt(closes[i], avg[i]) for i in range(n)]

    pos_long: List[Optional[float]] = [None] * n
    pos_short: List[Optional[float]] = [None] * n
    for i in range(n):
        if long_entry[i]:
            pos_long[i] = 1.0
        if long_exit[i]:                       # applied after, as in the reference
            pos_long[i] = 0.0
        if short_entry[i]:
            pos_short[i] = -1.0
        if short_exit[i]:
            pos_short[i] = 0.0

    # Signal = positions_long + positions_short, summed BEFORE the ffill: NaN in
    # either operand propagates. This is the quirk, kept.
    signal: List[Optional[float]] = [
        (pos_long[i] + pos_short[i])
        if (pos_long[i] is not None and pos_short[i] is not None) else None
        for i in range(n)]

    def _ffill(series):
        out, last = [], None
        for v in series:
            if v is not None:
                last = v
            out.append(last)
        return out

    return {"high": high, "low": low, "avg": avg,
            "long_entry": long_entry, "short_entry": short_entry,
            "long_exit": long_exit, "short_exit": short_exit,
            "positions_long": _ffill(pos_long), "positions_short": _ffill(pos_short),
            "signal": _ffill(signal)}


def stateful_signals(ref: dict) -> List[int]:
    """The exit-to-flat variant the reference's comments describe: a breakout
    opens a position, a close through the 55-bar mean closes it to FLAT (0).
    Same triggers, honest state machine."""
    n = len(ref["long_entry"])
    out, pos = [], 0
    for i in range(n):
        if ref["long_entry"][i]:
            pos = 1
        elif ref["short_entry"][i]:
            pos = -1
        elif pos == 1 and ref["long_exit"][i]:
            pos = 0
        elif pos == -1 and ref["short_exit"][i]:
            pos = 0
        out.append(pos)
    return out


def strategy_returns(closes: List[float], signal: List[Optional[float]]) -> dict:
    """`log(Close/Close.shift(1)) * Signal.shift(1)`, then `.cumsum()` — the
    reference's performance measure over the captured window. A backtest of the
    shadow on the same bars the signal was scored against, NOT a P&L claim."""
    rets, cum = [], 0.0
    for i in range(1, len(closes)):
        prev_sig = signal[i - 1]
        if prev_sig is None or closes[i - 1] <= 0 or closes[i] <= 0:
            rets.append(None)
            continue
        r = math.log(closes[i] / closes[i - 1]) * prev_sig
        cum += r
        rets.append(r)
    used = [r for r in rets if r is not None]
    return {"n": len(used), "cum_log_return": round(cum, 6),
            "mean_log_return": round(sum(used) / len(used), 8) if used else None}


def signal_turtle(*, closes: List[float], direction: Optional[str],
                  timeframe: str, cfg: dict = None) -> Optional[dict]:
    """The per-signal Turtle block. Pure — primitives in, JSON-able dict out.

    `agrees` is the decision-useful field: does the mechanical breakout system
    hold the same side as the channel's signal? A channel whose wins concentrate
    where `agrees` is true is riding a trend the Donchian system also sees; one
    whose wins are independent of it is contributing something the mechanical
    rule cannot."""
    cfg = {**DEFAULT_TURTLE, **(cfg or {})}
    window = max(2, int(cfg.get("window", 55)))
    cs = [float(c) for c in (closes or []) if c is not None]
    if len(cs) <= window:
        return None

    ref = reference_signals(cs, window)
    flat = stateful_signals(ref)
    last = len(cs) - 1
    sig = ref["signal"][last]
    sig_flat = flat[last]

    def _pos(v):
        if v is None:
            return "unknown"
        return "long" if v > 0 else "short" if v < 0 else "flat"

    agrees = None
    if direction and sig is not None:
        want = 1.0 if direction.upper() == "BUY" else -1.0
        agrees = (sig > 0 and want > 0) or (sig < 0 and want < 0)

    ch = channel(cs, window)
    return {
        "source": "PyPatel/Options-Trading-Strategies-in-Python :: Turtle Trading.py",
        "window": window, "timeframe": timeframe, "n_bars": len(cs),
        "close": round(cs[last], 5),
        "high": round(ch["high"], 5) if ch else None,
        "low": round(ch["low"], 5) if ch else None,
        "avg": round(ch["avg"], 5) if ch else None,
        "long_entry": bool(ref["long_entry"][last]),
        "short_entry": bool(ref["short_entry"][last]),
        "long_exit": bool(ref["long_exit"][last]),
        "short_exit": bool(ref["short_exit"][last]),
        "signal": sig,                       # reference value (stop-and-reverse)
        "signal_flat": sig_flat,             # documented intent (exits to flat)
        "position": _pos(sig),
        "position_flat": _pos(sig_flat),
        "diverges": (sig is not None and float(sig) != float(sig_flat)),
        "agrees": agrees,
        "backtest": strategy_returns(cs, ref["signal"]),
    }


# ===================== exit counterfactual (#170) =============================
# The per-signal block above is a snapshot at ENTRY. It cannot answer "should we
# have got out when the trend broke?", because nothing tracks the Turtle over a
# trade's life. This replays the same series across the holding period and asks
# where a flip-driven exit would have closed the position instead.
#
# SHADOW BY CONSTRUCTION: a backtest over bars we already fetch. It moves no
# stop and closes no position — it exists to earn (or refuse) the right to wire
# a Turtle exit into the live SL engine later.

def _bar_time(b) -> Optional[dt.datetime]:
    """A bar's timestamp as an aware UTC datetime, from `t` as datetime or ISO."""
    t = b.get("t") if isinstance(b, dict) else None
    if isinstance(t, dt.datetime):
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    if isinstance(t, str):
        try:
            d = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    return None


def _no_longer_backing(sig, direction: str, variant: str) -> bool:
    """Has the Turtle stopped holding this trade's side?

    The reference series never prints 0 (see module doc), so for it a flip means
    a sign change. The `signal_flat` variant does go flat, and flat is already
    'not backing you' — hence the <= / >=."""
    if sig is None:
        return False
    if (direction or "").upper() == "BUY":
        return sig <= 0 if variant == "signal_flat" else sig < 0
    return sig >= 0 if variant == "signal_flat" else sig > 0


def exit_counterfactual(*, bars, entry_time, exit_time, entry_price, sl_price,
                        actual_exit_price, direction: str, window: int = 55,
                        variant: str = "signal") -> Optional[dict]:
    """Where a Turtle flip would have closed this trade vs where it actually did.

    `bars` must extend far enough BEFORE `entry_time` for the 55-bar channel to
    be warm — the series is built over the whole array, then read inside the
    holding period, so the lookback is never truncated.

    Both R figures are computed on a PRICE basis off the same entry and the same
    risk distance, so they are comparable. Trade-level realized P&L is not mixed
    in: it is money across a multi-leg ladder with partial closes, which a single
    full-position counterfactual exit cannot be compared against. (Leg-level P&L
    is not used at all — CLAUDE.md §2.5. `actual_exit_price` is a lot-weighted
    average of leg close prices, which is a price, not a P&L attribution.)

    Returns None when the inputs cannot support the question at all.
    """
    if not bars or entry_time is None or exit_time is None:
        return None
    try:
        entry_price, sl_price = float(entry_price), float(sl_price)
        actual_exit_price = float(actual_exit_price)
    except (TypeError, ValueError):
        return None
    risk = abs(entry_price - sl_price)
    if risk <= 0:
        return None

    rows = [(t, float(b["c"])) for b, t in ((b, _bar_time(b)) for b in bars)
            if isinstance(b, dict) and b.get("c") is not None and t is not None]
    if len(rows) <= window:
        return None
    closes = [c for _t, c in rows]
    ref = reference_signals(closes, window)
    series = ref["signal"] if variant != "signal_flat" else stateful_signals(ref)

    long_side = (direction or "").upper() == "BUY"

    def _r(px: float) -> float:
        return ((px - entry_price) if long_side else (entry_price - px)) / risk

    flip_i = None
    for i, (t, _c) in enumerate(rows):
        if t <= entry_time or t > exit_time:      # only inside the holding period
            continue
        if _no_longer_backing(series[i], direction, variant):
            flip_i = i
            break

    actual_r = _r(actual_exit_price)
    out = {"variant": variant, "window": window,
           "n_bars": len(rows), "flipped": flip_i is not None,
           "actual_exit_price": round(actual_exit_price, 5),
           "actual_r": round(actual_r, 4)}
    if flip_i is None:
        # The Turtle never turned against the trade before it closed, so a
        # flip-driven exit would have changed nothing.
        out.update({"exit_time": None, "exit_price": None, "bars_held": None,
                    "counterfactual_r": round(actual_r, 4), "delta_r": 0.0})
        return out
    t, px = rows[flip_i]
    cf_r = _r(px)
    out.update({"exit_time": t.isoformat(), "exit_price": round(px, 5),
                "bars_held": sum(1 for tt, _ in rows if entry_time < tt <= t),
                "counterfactual_r": round(cf_r, 4),
                "delta_r": round(cf_r - actual_r, 4)})
    return out


def turtle_estimator(ctx) -> Optional[dict]:
    """SHADOW / measure-before-gate. Runs the Donchian breakout over the analytics
    price window and records what it would be holding at signal time, plus whether
    that agrees with the channel's direction."""
    conf = getattr(ctx, "config", None)
    cfg = conf.get("turtle") if isinstance(conf, dict) else None
    if isinstance(cfg, dict) and not cfg.get("enabled", True):
        return None
    return signal_turtle(closes=ctx.closes, direction=ctx.direction,
                         timeframe=ctx.timeframe, cfg=cfg)
