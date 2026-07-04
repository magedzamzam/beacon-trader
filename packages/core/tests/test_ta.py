"""Unit tests for the pure TA indicators (no DB / network)."""
from beacon_core.ta import indicators as I
from beacon_core.ta.features import timeframe_features


def test_sma():
    assert I.sma([1, 2, 3, 4, 5], 5) == 3
    assert I.sma([1, 2], 5) is None


def test_ema_tracks_and_needs_history():
    assert I.ema([1, 2], 5) is None
    flat = [10.0] * 30
    assert abs(I.ema(flat, 10) - 10.0) < 1e-9        # EMA of a flat line is the line


def test_rsi_extremes():
    up = list(range(1, 40))                          # strictly increasing
    assert I.rsi([float(x) for x in up], 14) == 100.0
    down = list(range(40, 1, -1))
    assert I.rsi([float(x) for x in down], 14) == 0.0


def test_macd_shape_and_cross():
    closes = [float(x) for x in range(1, 80)]        # steady uptrend
    m = I.macd(closes)
    assert set(m) == {"macd", "signal", "hist", "cross"}
    assert m["macd"] > 0                             # fast EMA above slow in an uptrend


def test_atr_positive():
    n = 30
    highs = [10.0 + i for i in range(n)]
    lows = [9.0 + i for i in range(n)]
    closes = [9.5 + i for i in range(n)]
    a = I.atr(highs, lows, closes, 14)
    assert a is not None and a > 0


def test_support_resistance_brackets_price():
    highs = [10, 12, 11, 15, 13, 16, 14, 18, 15, 12, 11, 13]
    lows = [8, 9, 8, 11, 10, 12, 11, 13, 12, 9, 8, 10]
    sup, res = I.support_resistance([float(h) for h in highs], [float(l) for l in lows], 12.5)
    if sup is not None:
        assert sup < 12.5
    if res is not None:
        assert res > 12.5


def test_timeframe_features_needs_min_bars():
    bars = [{"o": 100, "h": 101, "l": 99, "c": 100} for _ in range(10)]
    assert timeframe_features(bars, None) is None


def test_timeframe_features_full():
    bars = [{"o": 100 + i * 0.1, "h": 100.5 + i * 0.1, "l": 99.5 + i * 0.1,
             "c": 100 + i * 0.1} for i in range(120)]
    f = timeframe_features(bars, None)
    assert f is not None
    assert f["above_ema200"] is None or isinstance(f["above_ema200"], bool)  # 200 needs 200 bars
    assert isinstance(f["rsi14"], float)
    assert "macd" in f and "atr14" in f
