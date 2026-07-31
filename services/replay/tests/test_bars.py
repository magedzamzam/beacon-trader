"""Sided price semantics + the series. Getting the side wrong is worth about a
spread per trade, which is the same order of magnitude as the edges being
measured — so these are asserted, not assumed."""
from __future__ import annotations

import datetime as dt

from harness import bars as B
from conftest import T0, bar, path_bars, series


def test_buy_limit_needs_the_ask_to_fall_to_it():
    # mid low 4000.0 -> bid 3999.9 / ask 4000.1. The BID reached 4000 but the
    # ASK — which is what a BUY pays — did not.
    b = bar(T0, 4001, 4002, 4000, 4001)
    assert B.limit_touched("BUY", 4000.0, b) is False
    assert B.limit_touched("BUY", 4000.1, b) is True


def test_sell_limit_needs_the_bid_to_rise_to_it():
    b = bar(T0, 4001, 4002, 4000, 4001)
    assert B.limit_touched("SELL", 4002.0, b) is False   # bid high is 4001.9
    assert B.limit_touched("SELL", 4001.9, b) is True


def test_buy_stop_triggers_on_the_ask():
    b = bar(T0, 4001, 4002, 4000, 4001)                  # ask high 4002.1
    assert B.stop_triggered("BUY", 4002.1, b) is True
    assert B.stop_triggered("BUY", 4002.2, b) is False


def test_sell_stop_triggers_on_the_bid():
    b = bar(T0, 4001, 4002, 4000, 4001)                  # bid low 3999.9
    assert B.stop_triggered("SELL", 3999.9, b) is True
    assert B.stop_triggered("SELL", 3999.8, b) is False


def test_exit_sides_are_the_mirror_of_entry_sides():
    b = bar(T0, 4001, 4010, 3990, 4001)
    # A BUY exits by SELLING at the bid: TP needs high_bid, SL needs low_bid.
    assert B.tp_touched("BUY", 4009.9, b) is True
    assert B.tp_touched("BUY", 4010.0, b) is False
    assert B.sl_touched("BUY", 3989.9, b) is True
    assert B.sl_touched("BUY", 3989.8, b) is False
    # A SELL exits by BUYING at the ask.
    assert B.tp_touched("SELL", 3990.1, b) is True
    assert B.tp_touched("SELL", 3990.0, b) is False
    assert B.sl_touched("SELL", 4010.1, b) is True


def test_favourable_extreme_uses_the_closable_side():
    """An MFE must never claim a level the TRADEABLE side never reached — that
    is the #160 bug, and it is what ratchets a stop to breakeven on a mid."""
    b = bar(T0, 4001, 4010, 3990, 4001)
    assert B.favourable_extreme("BUY", b) == b.high_bid
    assert B.favourable_extreme("SELL", b) == b.low_ask
    assert B.adverse_extreme("BUY", b) == b.low_bid
    assert B.adverse_extreme("SELL", b) == b.high_ask


def test_slippage_sign_is_always_adverse():
    assert B.adverse("BUY", 0.5) == 0.5        # a BUY pays MORE
    assert B.adverse("SELL", 0.5) == -0.5      # a SELL receives LESS


def test_index_at_skips_a_bar_already_in_progress():
    s = series([4000, 4001, 4002])
    assert s.index_at(T0) == 0
    assert s.index_at(T0 + dt.timedelta(seconds=30)) == 1     # bar 0 already open
    assert s.index_at(T0 + dt.timedelta(minutes=1)) == 1
    assert s.index_at(T0 + dt.timedelta(minutes=9)) == 3      # past the end


def test_window_is_bounded_by_the_horizon():
    s = series(list(range(4000, 4010)))
    assert len(s.window(T0, 3)) == 3
    assert len(s.window(T0, 999)) == 10


def test_resample_buckets_onto_the_utc_grid_not_the_series_start():
    """Anchoring to the series start would make the same minute land in a
    different 4h bucket depending on the run's date range — determinism dies
    quietly that way."""
    start = dt.datetime(2026, 7, 6, 3, 50, tzinfo=dt.timezone.utc)
    bars = path_bars([4000] * 30, start=start)
    out = B.resample(bars, "1h")
    assert [b.ts.hour for b in out] == [3, 4]
    assert out[0].ts.minute == 0


def test_resample_ohlc_is_the_bucket_extreme():
    bars = path_bars([(4000, 4005, 3995, 4001), (4001, 4010, 4000, 4009),
                      (4009, 4012, 4008, 4011)], start=T0)
    out = B.resample(bars, "5m")
    assert len(out) == 1
    assert out[0].open == 4000 and out[0].close == 4011
    assert out[0].high == 4012 and out[0].low == 3995


def test_resample_does_not_forward_fill_a_gap():
    """Weekend gaps are ABSENT rows. An invented flat bar would feed a real
    number into an indicator with no data behind it."""
    a = path_bars([4000, 4001], start=T0)
    b = path_bars([4002], start=T0 + dt.timedelta(hours=6))
    out = B.resample(a + b, "1h")
    assert len(out) == 2                       # not 7
    assert (out[1].ts - out[0].ts).total_seconds() == 6 * 3600


def test_coverage_reports_the_suspect_exclusion():
    s = B.BarSeries(path_bars([4000, 4001]), suspect_excluded=17)
    cov = s.coverage()
    assert cov["n_bars"] == 2 and cov["suspect_excluded"] == 17
    assert cov["first"] and cov["last"]
