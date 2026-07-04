"""Unit tests for the TA indicators, registry, and config-driven features."""
from beacon_core.ta import indicators as I
from beacon_core.ta import registry as R
from beacon_core.ta.features import compute_timeframe


# ---- core indicators ----
def test_sma_ema():
    assert I.sma([1, 2, 3, 4, 5], 5) == 3
    assert I.sma([1, 2], 5) is None
    assert abs(I.ema([10.0] * 30, 10) - 10.0) < 1e-9


def test_rsi_extremes():
    assert I.rsi([float(x) for x in range(1, 40)], 14) == 100.0
    assert I.rsi([float(x) for x in range(40, 1, -1)], 14) == 0.0


def test_macd_uptrend_positive():
    m = I.macd([float(x) for x in range(1, 80)])
    assert set(m) == {"macd", "signal", "hist", "cross"} and m["macd"] > 0


def test_atr_positive():
    n = 30
    a = I.atr([10.0 + i for i in range(n)], [9.0 + i for i in range(n)],
              [9.5 + i for i in range(n)], 14)
    assert a is not None and a > 0


# ---- extended library ----
def test_bollinger_brackets_price():
    closes = [100.0 + (i % 5) for i in range(40)]
    b = I.bollinger(closes, 20, 2.0)
    assert b["lower"] <= b["middle"] <= b["upper"]
    assert isinstance(b["above_upper"], bool)


def test_stochastic_range():
    n = 40
    highs = [10.0 + (i % 7) for i in range(n)]
    lows = [8.0 + (i % 5) for i in range(n)]
    closes = [9.0 + (i % 6) for i in range(n)]
    s = I.stochastic(highs, lows, closes, 14, 3)
    assert 0 <= s["k"] <= 100


def test_adx_and_cci_and_wr():
    n = 60
    highs = [10.0 + i * 0.5 for i in range(n)]
    lows = [9.0 + i * 0.5 for i in range(n)]
    closes = [9.5 + i * 0.5 for i in range(n)]
    a = I.adx(highs, lows, closes, 14)
    assert a is not None and 0 <= a["adx"] <= 100
    assert I.cci(highs, lows, closes, 20) is not None
    wr = I.williams_r(highs, lows, closes, 14)
    assert wr is None or -100 <= wr <= 0


def test_volume_indicators_need_volume():
    closes = [float(i) for i in range(30)]
    assert I.obv(closes, [None] * 30) is None
    assert I.obv(closes, [10.0] * 30) is not None


# ---- registry / config ----
def test_catalog_shape():
    cat = R.catalog()
    ids = {i["id"] for i in cat["indicators"]}
    assert {"rsi", "ema", "macd", "bbands", "adx", "fib"} <= ids
    assert "1h" in cat["timeframes"]


def test_compute_one_unknown_and_known():
    ctx = R.Ctx(closes=[float(i) for i in range(60)],
                highs=[float(i) + 1 for i in range(60)],
                lows=[float(i) - 1 for i in range(60)],
                volumes=[None] * 60, price=59.0)
    assert R.compute_one(ctx, {"id": "does_not_exist"}) is None
    key, out = R.compute_one(ctx, {"id": "ema", "params": {"period": 20}})
    assert key == "ema_20" and "value" in out


def test_sanitize_config_drops_and_clamps():
    san = R.sanitize_config({"timeframes": ["5m", "BAD", "1h"],
                             "indicators": [{"id": "rsi", "params": {"period": 999}},
                                            {"id": "nope"}]})
    assert san["timeframes"] == ["5m", "1h"]
    assert san["indicators"] == [{"id": "rsi", "params": {"period": 200}}]


def test_compute_timeframe_config_driven():
    bars = [{"o": 100 + i * 0.1, "h": 100.5 + i * 0.1, "l": 99.5 + i * 0.1,
             "c": 100 + i * 0.1, "v": 100} for i in range(120)]
    cfg = [{"id": "rsi", "params": {"period": 14}},
           {"id": "ema", "params": {"period": 50}},
           {"id": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
           {"id": "unknown", "params": {}}]
    f = compute_timeframe(bars, None, cfg)
    assert "rsi_14" in f and "ema_50" in f and "macd_12_26_9" in f
    assert not any("unknown" in k for k in f)
    assert compute_timeframe(bars[:10], None, cfg) is None      # too few bars
