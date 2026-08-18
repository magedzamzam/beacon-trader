"""Entry-time market context, reconstructed from candles with NO look-ahead.

The executor reads the live quote, the in-progress candle, a 1h ATR and — only
when a filtration rule asks for it — a TA block. The harness has to hand the
same engines the same shapes, built from bars that had already CLOSED at the
signal instant. Getting that boundary wrong is the classic backtest fraud: an
indicator computed on the bar the signal arrived in has already seen the move it
is being asked to predict.

The rule enforced here, everywhere, is: **strictly-prior bars only.**

  * `current_price`  the mid at the OPEN of the signal's own 1m bar — the first
                     price that exists at or after the signal instant.
  * `candle_high/low` the PREVIOUS completed 1m bar. `build_plan` uses these to
                     decide whether the market has already crossed an entry
                     (MARKET) or not (LIMIT); live they come from the
                     in-progress candle, which contains only the part of the
                     minute that has already happened. The previous completed
                     bar is the no-look-ahead analogue. Handing it the signal
                     bar's own full range would let the planner MARKET-fill on a
                     touch that has not occurred yet.
  * `atr` / `ta`     computed from resampled bars STRICTLY BEFORE the signal bar.

PURE — stdlib + beacon_core's pure TA. No DB, no clock, no broker.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from beacon_core.execution import strategy as ST
from beacon_core.ta.features import compute_timeframe
from beacon_core.ta.indicators import adx as _adx, atr as _atr

from . import bars as B

# Bars of history handed to an indicator. Mirrors the executor's
# `get_bars(max_bars=250)` so a replayed indicator sees the same window length
# the live one does — an EMA over 250 bars is not an EMA over 5000.
TA_LOOKBACK_BARS = 250

# The timeframe the staged engine's ATR comes from live (`_atr_on(..., "1h")`).
STAGED_ATR_TIMEFRAME = "1h"
ATR_PERIOD = 14


@dataclass
class MarketContext:
    """What one signal sees at its own instant."""
    bar_index: int
    signal_bar: B.Bar
    current_price: Decimal
    candle_high: Optional[Decimal]
    candle_low: Optional[Decimal]
    atr_1h: Optional[float]
    ta: Dict[str, Any] = field(default_factory=dict)
    adx: Dict[str, Any] = field(default_factory=dict)


class ContextBuilder:
    """Resamples once per timeframe for the whole series and then answers
    per-signal queries by bisecting the resampled index.

    Resampling 400k 1m bars per signal would dominate the run; resampling once
    and slicing is the same arithmetic done 800x less. The slice is always
    `[:i]` where `i` is the first bucket at or after the signal, so the bar the
    signal lives in is EXCLUDED — that exclusion is the no-look-ahead guarantee
    and it lives in exactly one place."""

    def __init__(self, series: B.BarSeries):
        self.series = series
        self._resampled: Dict[str, List[B.OhlcBar]] = {}
        self._ts: Dict[str, List[dt.datetime]] = {}

    def _frame(self, timeframe: str):
        if timeframe not in self._resampled:
            rs = B.resample(self.series.bars, timeframe)
            self._resampled[timeframe] = rs
            self._ts[timeframe] = [b.ts for b in rs]
        return self._resampled[timeframe], self._ts[timeframe]

    def closed_bars(self, timeframe: str, before: dt.datetime,
                    limit: int = TA_LOOKBACK_BARS) -> List[B.OhlcBar]:
        """The last `limit` buckets that had fully CLOSED before `before`.

        A bucket whose window contains `before` is dropped, not truncated: the
        registry's indicators read the newest bar as a completed one, and a
        partial bar would report a high the minute had not printed yet."""
        import bisect
        rs, ts = self._frame(timeframe)
        if not rs:
            return []
        minutes = B.timeframe_minutes(timeframe) or 1
        i = bisect.bisect_left(ts, before)
        # ts[i-1] is the newest bucket that OPENED before `before`; it has closed
        # only if its whole window is behind us.
        if i > 0 and ts[i - 1] + dt.timedelta(minutes=minutes) > before:
            i -= 1
        return rs[max(0, i - limit):i]

    # --- the pieces the executor plumbs -------------------------------------
    def atr(self, timeframe: str, before: dt.datetime,
            period: int = ATR_PERIOD) -> Optional[float]:
        win = self.closed_bars(timeframe, before, max(period * 5, 60))
        if len(win) < period + 1:
            return None
        return _atr([b.high for b in win], [b.low for b in win],
                    [b.close for b in win], period)

    def adx_block(self, timeframe: str, before: dt.datetime,
                  period: int = ATR_PERIOD) -> Optional[dict]:
        """`{adx, trending}` — the same shape and the same registry function the
        executor's `_adx_read` produces. Fail-open: None on a thin series."""
        win = self.closed_bars(timeframe, before, TA_LOOKBACK_BARS)
        if len(win) < period + 1:
            return None
        d = _adx([b.high for b in win], [b.low for b in win],
                 [b.close for b in win], period)
        if not d or d.get("adx") is None:
            return None
        return {"adx": d["adx"], "trending": bool(d.get("trending"))}

    def ta_block(self, rules, before: dt.datetime) -> Dict[str, Any]:
        """`ctx['ta'][timeframe][instance_key]` for every indicator the rule set
        references (#167), via the registry's own compute path (`compute_
        timeframe`) — no duplicated math, and nothing computed at all when no
        rule mentions TA."""
        reqs = ST.ta_rule_requirements(rules)
        if not reqs:
            return {}
        by_tf: Dict[str, list] = {}
        for r in reqs:
            by_tf.setdefault(r["timeframe"], []).append(r)
        out: Dict[str, Any] = {}
        for tf, items in by_tf.items():
            win = self.closed_bars(tf, before, TA_LOOKBACK_BARS)
            if not win:
                continue
            rows = [{"o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume}
                    for b in win]
            try:
                feats = compute_timeframe(
                    rows, None, [{"id": r["id"], "params": r["params"]} for r in items])
            except Exception:
                continue                        # fail-open, exactly as the executor
            block = {k: v for k, v in (feats or {}).items() if not k.startswith("_")}
            if block:
                out[tf] = block
        return out

    # --- the whole context ---------------------------------------------------
    def build(self, when: dt.datetime, *, filter_rules=None,
              need_staged_atr: bool = False) -> Optional[MarketContext]:
        """The context for a signal timestamped `when`, or None when the candle
        store does not cover it (which is an EXCLUSION to be reported, never a
        silently-skipped signal)."""
        i = self.series.index_at(when)
        if i >= len(self.series):
            return None
        bar = self.series[i]
        prev = self.series[i - 1] if i > 0 else None
        atr_1h = self.atr(STAGED_ATR_TIMEFRAME, when) if need_staged_atr else None
        rules = filter_rules or []
        ta = self.ta_block(rules, when) if rules else {}
        adx: Dict[str, Any] = {}
        for tf in (ST.adx_rule_timeframes(rules) if rules else ()):
            blk = self.adx_block(tf, when)
            if blk is not None:
                adx[tf] = blk
        return MarketContext(
            bar_index=i, signal_bar=bar,
            current_price=Decimal(str(B.open_mid(bar))),
            candle_high=None if prev is None else Decimal(str(B.high_mid(prev))),
            candle_low=None if prev is None else Decimal(str(B.low_mid(prev))),
            atr_1h=atr_1h, ta=ta, adx=adx)


def filter_ctx(mc: MarketContext, *, sessions=None) -> dict:
    """The dict `evaluate_filter_rules` reads. Same keys the executor builds, so a
    rule that fires live fires here — and one whose inputs are missing stays a
    no-op rather than deleting the signal (#164)."""
    ctx: dict = {"price": float(mc.current_price)}
    # The signal's own instant, for the `time_window` leaf (#214). The replayed
    # analogue of the executor's wall clock: live, a rule is evaluated within
    # seconds of ingest, so the signal bar's timestamp is the same reading.
    ts = getattr(mc.signal_bar, "ts", None)
    if ts is not None:
        ctx["ts"] = ts
    if sessions:
        ctx["sessions"] = list(sessions)
    if mc.adx:
        ctx["adx"] = mc.adx
    if mc.ta:
        ctx["ta"] = mc.ta
    return ctx
