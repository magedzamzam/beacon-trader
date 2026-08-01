"""Candle access + SIDED price semantics for the replay harness (#169).

The candle store carries full bid AND ask OHLC, which is what makes exact fill
semantics possible instead of a mid price plus a spread guess. Every predicate a
simulated order needs is defined here, once, so `fills.py` never has to remember
which column decides a BUY stop:

    direction  enter (we pay)  exit (we receive)  TP touched      SL touched
    BUY        ask             bid                high_bid >= tp  low_bid  <= sl
    SELL       bid             ask                low_ask  <= tp  high_ask >= sl

A resting BUY LIMIT fills when the ASK falls to the level (`low_ask <= level`);
a BUY STOP triggers when the ASK rises through it (`high_ask >= level`). The
mirror holds for SELL against the bid. Using the wrong side is worth about a
spread per trade, which is the same order of magnitude as the edges being
measured — hence one module, not an inline comparison per call site.

Spread is therefore modelled INTRINSICALLY (it is the gap between the two
series), never from `candles.spread_nominal`: that column is the CLOSE spread
and disagrees with the open/high/low spreads on the same bar (#169 comment).

PURE — stdlib only, no DB/clock — so the whole simulator runs on a bare box.
Shares the sided convention with `beacon_core.analysis.excursion`, deliberately:
the excursion labeller and the replay harness must not disagree about whether a
level was reached.
"""
from __future__ import annotations

import bisect
import datetime as dt
from typing import List, NamedTuple, Optional, Sequence

BUY = "BUY"
SELL = "SELL"


class Bar(NamedTuple):
    """One candle. Prices are floats — the simulator is comparing levels, not
    settling money, and Decimal per bar over a 400k-bar window is not worth it.
    Money math converts back to Decimal at the leg level."""
    ts: dt.datetime
    open_bid: float
    open_ask: float
    high_bid: float
    high_ask: float
    low_bid: float
    low_ask: float
    close_bid: float
    close_ask: float
    volume: Optional[float] = None


def mid(a: float, b: float) -> float:
    return (a + b) / 2.0


def open_mid(bar: Bar) -> float:
    return mid(bar.open_bid, bar.open_ask)


def close_mid(bar: Bar) -> float:
    return mid(bar.close_bid, bar.close_ask)


def high_mid(bar: Bar) -> float:
    return mid(bar.high_bid, bar.high_ask)


def low_mid(bar: Bar) -> float:
    return mid(bar.low_bid, bar.low_ask)


# --- sided predicates ---------------------------------------------------------
def entry_price_now(direction: str, bar: Bar) -> float:
    """What a MARKET order pays if it is sent at the OPEN of this bar: the ask
    for a BUY, the bid for a SELL."""
    return bar.open_ask if direction == BUY else bar.open_bid


def limit_touched(direction: str, level: float, bar: Bar) -> bool:
    """Did a resting LIMIT at `level` become fillable inside this bar? A BUY limit
    needs the ASK to fall to it; a SELL limit needs the BID to rise to it."""
    return bar.low_ask <= level if direction == BUY else bar.high_bid >= level


def stop_triggered(direction: str, level: float, bar: Bar) -> bool:
    """Did a resting STOP at `level` trigger inside this bar? A BUY stop sits
    ABOVE the market and fires when the ASK trades up through it; a SELL stop
    sits below and fires when the BID trades down through it."""
    return bar.high_ask >= level if direction == BUY else bar.low_bid <= level


def entry_shortfall(direction: str, order_type: str, level: float,
                    bar: Bar) -> Optional[float]:
    """How far the fillable side of `bar` came SHORT of `level`, in points (#185).

    Zero or negative means the order was fillable in this bar; positive is the
    gap that stopped it. It is the same comparison `limit_touched` /
    `stop_triggered` make, expressed as a DISTANCE rather than a verdict — same
    sided semantics, one place, so a diagnostic cannot drift from the predicate
    it is diagnosing.

    Why it is worth measuring: 34 of the gate's 72 disagreements are legs the
    simulator expired while live filled (#185, defect A). That is either a spread
    the candle feed prints wider than Capital.com's was, or a TTL/window problem
    — and the two have completely different fixes. A miss clustering at a
    fraction of a point is the spread; a large one is not. Guessing which,
    without the distribution, is how a modelling assumption becomes folklore."""
    if order_type == "LIMIT":
        return (bar.low_ask - level) if direction == BUY else (level - bar.high_bid)
    if order_type == "STOP":
        return (level - bar.high_ask) if direction == BUY else (bar.low_bid - level)
    return None                          # MARKET always fills; there is no gap


def gap_at_open(direction: str, order_type: str, level: float,
                bar: Bar) -> Optional[float]:
    """How much the "fills AT its level" rule cost (or saved) on THIS fill (#190).

    The harness fills a LIMIT at its level and never better, deliberately: a bar
    that gapped through a resting BUY limit would in reality have filled at the
    better open, and crediting that improvement is free money it cannot verify
    (`fills.py`). That refusal is not free — it puts a POSITIVE mean into
    `fill_adverse` all by itself, and spread must not be charged for it.

    So it is measured rather than argued about. Compare the level the simulator
    fills at against the price the bar's OPEN would have given on the side we
    transact, and return it only when the bar OPENED already through the level —
    the one case where live would genuinely have transacted at the open instead.

    ADVERSE-SIGNED, the same convention as `validate._adverse_delta`:
      * POSITIVE — a LIMIT whose bar opened BETTER than the level. The simulator
        declines the improvement, so its fill is worse than live's would be.
      * NEGATIVE — a STOP whose bar opened BEYOND the trigger. Live pays the gap;
        the simulator fills at the trigger, so it is FLATTERED here. The same
        modelling choice cuts both ways and both directions are reported.
      * None — no gap (the level was reached inside the bar, so live would have
        filled at the level too), or a MARKET order, which already fills at the
        open and has nothing to forgo.

    Measured against the ORDER'S LEVEL, before slippage: slippage is a separate,
    operator-configured penalty, and folding it in here would attribute a
    deliberate cost to the fill model."""
    if order_type not in ("LIMIT", "STOP"):
        return None
    px = entry_price_now(direction, bar)          # the side we pay, at the open
    d = (level - px) if direction == BUY else (px - level)
    if order_type == "LIMIT":
        return d if d > 0 else None               # opened better; improvement forgone
    return d if d < 0 else None                   # opened worse; trigger fill flatters


def tp_touched(direction: str, tp: float, bar: Bar) -> bool:
    """We exit a BUY by SELLING at the bid, so a BUY take-profit needs
    `high_bid >= tp`. The mirror for a SELL."""
    return bar.high_bid >= tp if direction == BUY else bar.low_ask <= tp


def sl_touched(direction: str, sl: float, bar: Bar) -> bool:
    return bar.low_bid <= sl if direction == BUY else bar.high_ask >= sl


def favourable_extreme(direction: str, bar: Bar) -> float:
    """The best price this bar printed on the side we would CLOSE on — the only
    honest basis for a max-favourable excursion. A mid-price high is a level the
    tradeable side may never have reached, and ratcheting a stop to breakeven on
    a mid is exactly the bug #160 fixed live."""
    return bar.high_bid if direction == BUY else bar.low_ask


def adverse_extreme(direction: str, bar: Bar) -> float:
    return bar.low_bid if direction == BUY else bar.high_ask


def adverse(direction: str, points: float) -> float:
    """Signed price offset that makes a fill WORSE by `points` — the sign
    slippage is applied with."""
    return points if direction == BUY else -points


# --- the series ---------------------------------------------------------------
class BarSeries:
    """An ascending, gap-tolerant 1m series with O(log n) time lookup.

    Weekend/holiday gaps are ABSENT ROWS, not zero-filled rows (the correct
    form), so index arithmetic must never be used as a clock: `index_at` bisects
    the real timestamps. `n_expected_missing` reports the audit the harness owes
    its results — a gap has to be KNOWN, not silently treated as flat price."""

    __slots__ = ("bars", "_ts", "symbol", "timeframe", "suspect_excluded")

    def __init__(self, bars: Sequence[Bar], *, symbol: str = "XAUUSD",
                 timeframe: str = "1m", suspect_excluded: int = 0):
        self.bars: List[Bar] = list(bars)
        self._ts = [b.ts for b in self.bars]
        self.symbol = symbol
        self.timeframe = timeframe
        # Bars dropped by the `quality='ok'` filter. Reported on every result as a
        # caveat, never buried (#169: flag, do not auto-repair a crossed quote).
        self.suspect_excluded = int(suspect_excluded)

    def __len__(self) -> int:
        return len(self.bars)

    def __getitem__(self, i):
        return self.bars[i]

    @property
    def first_ts(self) -> Optional[dt.datetime]:
        return self._ts[0] if self._ts else None

    @property
    def last_ts(self) -> Optional[dt.datetime]:
        return self._ts[-1] if self._ts else None

    def index_at(self, when: dt.datetime) -> int:
        """Index of the first bar that OPENED at or after `when`. A bar already in
        progress is skipped rather than credited with a wick that predates us —
        the same rule `excursion_store._window` uses."""
        return bisect.bisect_left(self._ts, when)

    def window(self, start: dt.datetime, horizon_bars: int) -> List[Bar]:
        i = self.index_at(start)
        return self.bars[i:i + max(0, int(horizon_bars))]

    def coverage(self) -> dict:
        return {"n_bars": len(self.bars),
                "first": self.first_ts.isoformat() if self.first_ts else None,
                "last": self.last_ts.isoformat() if self.last_ts else None,
                "suspect_excluded": self.suspect_excluded}


# --- gap audit ----------------------------------------------------------------
# A trading day materially below the median is a HOLE, not a quiet session.
THIN_DAY_FRACTION = 0.5
_ONE_DAY = dt.timedelta(days=1)


def daily_gaps(bars: Sequence[Bar], *, frm=None,
               thin_fraction: float = THIN_DAY_FRACTION) -> dict:
    """Per-day bar counts, and the weekdays that are missing or thin (#169 §3).

    Aggregate density cannot see this. A window can be 70% populated — exactly
    what a five-day-a-week instrument should be — while one Tuesday is absent
    entirely, and every signal on that Tuesday would then replay against a price
    series that simply stops, resolving by horizon rather than by the market.
    The issue is explicit that gaps must be KNOWN rather than silently treated
    as flat price, so they are counted rather than inferred from a total.

    Weekends are absent by design and are excluded from the median and from the
    missing list. A weekday with NO bars is missing; one below `thin_fraction`
    of the weekday median is thin. Pure."""
    per_day: dict = {}
    for b in bars:
        if frm is not None and b.ts < frm:
            continue
        per_day[b.ts.date()] = per_day.get(b.ts.date(), 0) + 1
    if not per_day:
        return {"n_days": 0, "median_weekday_bars": None,
                "missing_weekdays": [], "thin_weekdays": []}

    counts = sorted(n for d, n in per_day.items() if d.weekday() < 5)
    median = counts[len(counts) // 2] if counts else 0

    missing, thin = [], []
    day, end = min(per_day), max(per_day)
    while day <= end:
        if day.weekday() < 5:                          # Mon-Fri
            n = per_day.get(day, 0)
            if n == 0:
                missing.append(day.isoformat())
            elif median and n < thin_fraction * median:
                thin.append({"date": day.isoformat(), "n_bars": n})
        day = day + _ONE_DAY
    return {"n_days": len(per_day), "median_weekday_bars": median or None,
            "missing_weekdays": missing, "thin_weekdays": thin,
            "note": ("Weekends are absent by design and excluded. A MISSING or "
                     "THIN weekday is a hole: signals on it replay against a "
                     "price series that stops, so they resolve by horizon "
                     "rather than by the market.")}


# --- resampling (for the TA context the filtration pillar reads) --------------
_TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240,
               "1d": 1440}


def timeframe_minutes(tf: str) -> Optional[int]:
    return _TF_MINUTES.get(tf)


class OhlcBar(NamedTuple):
    """A resampled MID bar. Indicators are defined on a single price series, and
    the registry takes one; mid is the neutral choice and the harness never uses
    these for fills — only `Bar` decides a fill."""
    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float]


def _bucket(ts: dt.datetime, minutes: int) -> dt.datetime:
    """Floor a timestamp onto the timeframe grid. Anchored to the UTC day so a
    4h bucket lands on 00/04/08/… regardless of where the series starts —
    otherwise two runs over different date ranges would compute a different 4h
    ADX for the same minute, and determinism dies quietly."""
    day = ts.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((ts - day).total_seconds() // 60)
    return day + dt.timedelta(minutes=(elapsed // minutes) * minutes)


def resample(bars: Sequence[Bar], timeframe: str) -> List[OhlcBar]:
    """1m bid/ask bars -> completed MID OHLC bars at `timeframe`.

    A gap simply produces no bucket — the series is not forward-filled, because
    an invented flat bar would feed a real number into an indicator that has no
    data behind it. Callers get fewer bars and the indicator returns None, which
    is the fail-open posture the filtration evaluator already expects."""
    minutes = _TF_MINUTES.get(timeframe)
    if not minutes or not bars:
        return []
    out: List[OhlcBar] = []
    cur_key = None
    o = h = l = c = None
    vol = 0.0
    have_vol = False
    for b in bars:
        key = _bucket(b.ts, minutes)
        px_o, px_h, px_l, px_c = open_mid(b), high_mid(b), low_mid(b), close_mid(b)
        if key != cur_key:
            if cur_key is not None:
                out.append(OhlcBar(cur_key, o, h, l, c, vol if have_vol else None))
            cur_key, o, h, l, c = key, px_o, px_h, px_l, px_c
            vol, have_vol = 0.0, False
        else:
            h = max(h, px_h)
            l = min(l, px_l)
            c = px_c
        if b.volume is not None:
            vol += float(b.volume)
            have_vol = True
    if cur_key is not None:
        out.append(OhlcBar(cur_key, o, h, l, c, vol if have_vol else None))
    return out
