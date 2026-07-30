"""Unit tests for the broadened shadow TA set (#166).

Every indicator here is a clean-room reimplementation from the public formula —
FinTA is LGPL-3.0 and pandas-based, so nothing is copied from it and no new
dependency enters `beacon_core`. The tests below pin each one against a
hand-computable fixture (a constant series, a linear ramp, or an arithmetic
identity) rather than against another library's output.
"""
import math

import pytest

from beacon_core.ta import indicators as I
from beacon_core.ta import registry as R


def _ramp(n, start=100.0, step=1.0):
    return [start + step * i for i in range(n)]


def _ohlc(n, start=100.0, step=1.0, spread=1.0):
    closes = _ramp(n, start, step)
    return ([c + spread for c in closes], [c - spread for c in closes], closes)


# ---- pivots: the P0 gap — implemented but previously unreachable -------------
def test_pivots_classic_hand_checked():
    p = I.pivots(110.0, 90.0, 100.0)           # P = (110+90+100)/3 = 100
    assert p["p"] == pytest.approx(100.0)
    assert p["r1"] == pytest.approx(110.0)     # 2P - L
    assert p["s1"] == pytest.approx(90.0)      # 2P - H
    assert p["r2"] == pytest.approx(120.0)     # P + (H-L)
    assert p["s2"] == pytest.approx(80.0)


def test_pivot_fib_hand_checked():
    p = I.pivot_fib(110.0, 90.0, 100.0)        # P = 100, range = 20
    assert p["p"] == pytest.approx(100.0)
    assert p["r1"] == pytest.approx(107.64)    # P + 0.382*20
    assert p["s1"] == pytest.approx(92.36)
    assert p["r2"] == pytest.approx(112.36)    # P + 0.618*20
    assert p["s2"] == pytest.approx(87.64)
    assert p["r3"] == pytest.approx(120.0)     # P + 1.000*20
    assert p["s3"] == pytest.approx(80.0)


def test_pivots_registered_and_reachable_from_config():
    """The whole point of #166's P0: `pivots` existed in indicators.py but was
    absent from REGISTRY, so no config could select it and it was never captured."""
    ids = {s["id"] for s in R.REGISTRY}
    assert {"pivots", "pivot_fib"} <= ids
    san = R.sanitize_config({"timeframes": ["1h"],
                             "indicators": [{"id": "pivots"}, {"id": "pivot_fib"}]})
    assert [i["id"] for i in san["indicators"]] == ["pivots", "pivot_fib"]


def test_pivots_use_the_previous_completed_bar():
    """Pivots are defined on the PREVIOUS period; the newest bar is still forming."""
    highs, lows, closes = _ohlc(40)
    ctx = R.Ctx(closes=closes, highs=highs, lows=lows, volumes=[None] * 40,
                price=closes[-1])
    key, out = R.compute_one(ctx, {"id": "pivots", "params": {}})
    expected = I.pivots(highs[-2], lows[-2], closes[-2])
    assert key == "pivots"
    assert out["p"] == pytest.approx(expected["p"], abs=1e-4)
    assert out["above_p"] is True and out["nearest"] in expected


# ---- trend ------------------------------------------------------------------
def test_parabolic_sar_trails_below_an_uptrend():
    highs, lows, closes = _ohlc(60)
    s = I.parabolic_sar(highs, lows)
    assert s["trend"] == "up"
    assert s["value"] < lows[-1]               # a long stop sits under price


def test_parabolic_sar_flips_on_reversal():
    up_h, up_l, _ = _ohlc(40)
    dn_h = up_h + [up_h[-1] - 1.0 * i for i in range(1, 41)]
    dn_l = up_l + [up_l[-1] - 1.0 * i for i in range(1, 41)]
    s = I.parabolic_sar(dn_h, dn_l)
    assert s["trend"] == "down" and s["value"] > dn_h[-1]


def test_vortex_positive_in_an_uptrend():
    highs, lows, closes = _ohlc(60)
    v = I.vortex(highs, lows, closes, 14)
    assert v["plus"] > v["minus"] and v["bullish"] is True
    assert v["diff"] == pytest.approx(v["plus"] - v["minus"])


def test_vortex_needs_history():
    assert I.vortex([1.0, 2.0], [0.0, 1.0], [0.5, 1.5], 14) is None


def test_ichimoku_lines_and_cloud():
    n = 120
    highs, lows, closes = _ohlc(n)
    ich = I.ichimoku(highs, lows, closes)
    # tenkan = midpoint of the last 9 bars' extremes; on a +1/bar ramp with ±1
    # spread that is (last_high + high_9_ago - 2) / 2 ... just check it directly.
    assert ich["tenkan"] == pytest.approx((max(highs[-9:]) + min(lows[-9:])) / 2)
    assert ich["kijun"] == pytest.approx((max(highs[-26:]) + min(lows[-26:])) / 2)
    # A steady uptrend puts price above the cloud that actually applies now.
    assert ich["above_cloud"] is True and ich["in_cloud"] is False


def test_ichimoku_cloud_none_without_enough_shift_history():
    highs, lows, closes = _ohlc(55)          # >= senkou(52) but < 52 + 26
    ich = I.ichimoku(highs, lows, closes)
    assert ich["tenkan"] is not None and ich["cloud_a"] is None


# ---- MA variants: constant series is the identity fixture -------------------
@pytest.mark.parametrize("fn", [I.dema, I.tema, I.hma, I.zlema, I.kama])
def test_ma_variants_return_the_constant(fn):
    assert fn([100.0] * 200, 20) == pytest.approx(100.0, abs=1e-6)


@pytest.mark.parametrize("fn", [I.dema, I.tema, I.hma, I.zlema])
def test_ma_variants_lag_less_than_ema_on_a_ramp(fn):
    """Every one of these exists to cut EMA's lag; on a monotone ramp that means
    sitting strictly closer to the newest price than a same-period EMA."""
    vals = _ramp(300)
    e, v = I.ema(vals, 20), fn(vals, 20)
    assert abs(vals[-1] - v) < abs(vals[-1] - e)


def test_ma_variants_need_history():
    assert I.dema([1.0] * 5, 20) is None
    assert I.tema([1.0] * 5, 20) is None
    assert I.hma([1.0] * 5, 20) is None
    assert I.zlema([1.0] * 5, 20) is None
    assert I.kama([1.0] * 5, 20) is None


def test_kama_adapts_to_efficiency_not_just_to_period():
    """The efficiency ratio is the whole point. Against a same-period EMA, KAMA
    must track a clean trend MORE closely (high ER -> fast constant) and a zig-zag
    of the same bar amplitude LESS closely, i.e. sit nearer the chop's mean."""
    trend = _ramp(120, 100.0, 1.0)
    chop = [100.0 + (i % 2) for i in range(120)]
    assert abs(trend[-1] - I.kama(trend)) < abs(trend[-1] - I.ema(trend, 10))
    assert abs(100.5 - I.kama(chop)) < abs(100.5 - I.ema(chop, 10))


# ---- momentum ---------------------------------------------------------------
def test_cmo_saturates_at_both_extremes():
    assert I.cmo(_ramp(60), 14) == pytest.approx(100.0)
    assert I.cmo(_ramp(60, 200.0, -1.0), 14) == pytest.approx(-100.0)


def test_tsi_signs_with_the_trend():
    up, down = _ramp(200), _ramp(200, 400.0, -1.0)
    assert I.tsi(up) == pytest.approx(100.0, abs=1e-6)
    assert I.tsi(down) == pytest.approx(-100.0, abs=1e-6)


def test_tsi_needs_history():
    assert I.tsi(_ramp(20)) is None


def test_ultimate_osc_bounded_and_high_when_closes_lead():
    """Buying pressure is close-minus-the-period-low, so a trend that closes at the
    top of every bar reads high. A symmetric ±spread ramp reads exactly 50 — the
    close sits mid-range — which is why this fixture is deliberately lopsided."""
    closes = _ramp(80)
    highs = [c + 0.2 for c in closes]
    lows = [c - 2.0 for c in closes]
    u = I.ultimate_osc(highs, lows, closes)
    assert 0.0 <= u <= 100.0 and u > 80.0


def test_ultimate_osc_symmetric_bar_reads_neutral():
    highs, lows, closes = _ohlc(80)
    assert I.ultimate_osc(highs, lows, closes) == pytest.approx(50.0)


def test_awesome_osc_hand_checked_on_a_ramp():
    """median price = i on this fixture, so AO = SMA(5) - SMA(34) reduces to
    (x-2) - (x-16.5) = 14.5 exactly, independent of where the ramp ends."""
    n = 60
    highs = lows = [float(i) for i in range(n)]
    assert I.awesome_osc(highs, lows, 5, 34) == pytest.approx(14.5)


def test_fisher_transform_bounded_pair():
    highs, lows, closes = _ohlc(60)
    f = I.fisher_transform(highs, lows, 9)
    assert f["value"] > 0                       # at the top of its range
    assert math.isfinite(f["value"]) and math.isfinite(f["signal"])


def test_fisher_transform_survives_a_flat_window():
    """A zero-range window would divide by zero if the (-1,1) clamp were absent."""
    f = I.fisher_transform([100.0] * 40, [100.0] * 40, 9)
    assert f["value"] == pytest.approx(0.0)


def test_elder_ray_brackets_the_ema():
    highs, lows, closes = _ohlc(60)
    er = I.elder_ray(highs, lows, closes, 13)
    assert er["bull"] > er["bear"]              # high is above low, by construction


# ---- volatility -------------------------------------------------------------
def test_true_range_hand_checked():
    # last bar: H=12 L=9, prev close 9 -> max(3, 3, 0) = 3
    assert I.true_range([10.0, 12.0], [8.0, 9.0], [9.0, 11.0]) == pytest.approx(3.0)
    assert I.true_range([10.0], [8.0], [9.0]) is None


def test_chandelier_exit_hangs_off_the_window_extremes():
    highs, lows, closes = _ohlc(60)
    ce = I.chandelier_exit(highs, lows, closes, 22, 3.0)
    a = I.atr(highs, lows, closes, 22)
    assert ce["long"] == pytest.approx(max(highs[-22:]) - 3.0 * a)
    assert ce["short"] == pytest.approx(min(lows[-22:]) + 3.0 * a)


def test_squeeze_on_when_bands_collapse_inside_the_channel():
    """Flat closes with wide bars: stddev is 0 so BB collapses to the mean, while
    ATR keeps the Keltner channel wide — the textbook squeeze."""
    n = 60
    closes = [100.0] * n
    highs, lows = [105.0] * n, [95.0] * n
    sq = I.squeeze(highs, lows, closes, 20, 2.0, 1.5)
    assert sq["on"] is True and sq["width_ratio"] == pytest.approx(0.0)


def test_squeeze_off_when_price_is_volatile():
    n = 120
    closes = [100.0 + 12.0 * ((i % 6) - 2.5) for i in range(n)]
    highs = [c + 0.1 for c in closes]
    lows = [c - 0.1 for c in closes]
    sq = I.squeeze(highs, lows, closes, 20, 2.0, 1.5)
    assert sq["on"] is False


def test_apz_band_is_ordered_and_symmetric():
    highs, lows, closes = _ohlc(80)
    z = I.apz(highs, lows, closes, 21, 2.0)
    assert z["lower"] < z["middle"] < z["upper"]
    assert (z["upper"] - z["middle"]) == pytest.approx(z["middle"] - z["lower"])


def test_typical_price_hand_checked():
    assert I.typical_price([12.0], [6.0], [9.0]) == pytest.approx(9.0)
    assert I.typical_price([], [], []) is None


# ---- registry contract ------------------------------------------------------
def _synthetic_ctx(n=300):
    """A deterministic wiggling series — enough bars and enough movement for every
    registry entry to have an opinion."""
    closes, highs, lows, opens = [], [], [], []
    price = 2000.0
    for i in range(n):
        o = price
        price += math.sin(i / 7.0) * 3.0 + 0.4          # drift + oscillation
        c = price
        h = max(o, c) + 1.2 + (i % 3) * 0.3
        l = min(o, c) - 1.2 - (i % 5) * 0.2
        opens.append(o); closes.append(c); highs.append(h); lows.append(l)
    return R.Ctx(closes=closes, highs=highs, lows=lows,
                 volumes=[100.0] * n, price=closes[-1], opens=opens)


def test_registry_ids_are_unique():
    ids = [s["id"] for s in R.REGISTRY]
    assert len(ids) == len(set(ids))


def test_every_registry_entry_computes_on_a_real_series():
    """Contract: an entry that is registered must produce output on a healthy
    series. A silent None here means the indicator is registered but dead — the
    exact failure mode #168 is about."""
    ctx = _synthetic_ctx()
    dead = []
    for spec in R.REGISTRY:
        params = {p["name"]: p["default"] for p in spec["params"]}
        res = R.compute_one(ctx, {"id": spec["id"], "params": params})
        if res is None or not res[1]:
            dead.append(spec["id"])
    assert dead == []


def test_declared_outputs_match_what_compute_returns():
    """The generic entry filter (#167) addresses `outputs` by name and the API
    validator 422s an unlisted field, so a drift between the declaration and the
    real dict would either hide a usable field or reject a valid rule."""
    ctx = _synthetic_ctx()
    mismatched = {}
    for spec in R.REGISTRY:
        params = {p["name"]: p["default"] for p in spec["params"]}
        res = R.compute_one(ctx, {"id": spec["id"], "params": params})
        actual, declared = set(res[1]), set(spec["outputs"])
        if actual != declared:
            mismatched[spec["id"]] = {"missing": sorted(actual - declared),
                                      "stale": sorted(declared - actual)}
    assert mismatched == {}


def test_every_registry_entry_declares_its_outputs_explicitly():
    assert all(s["id"] in R._OUTPUTS for s in R.REGISTRY)


def test_resolve_instance_is_the_shared_key_derivation():
    inst = R.resolve_instance("rsi", {"period": 999})       # clamped to the max
    assert inst["key"] == "rsi_200" and inst["params"] == {"period": 200}
    assert inst["outputs"] == ["value"]
    assert R.resolve_instance("rsi")["key"] == "rsi_14"     # defaults merged
    assert R.resolve_instance("nope") is None


def test_new_indicators_are_all_in_the_catalog():
    added = {"pivots", "pivot_fib", "psar", "vortex", "ichimoku", "dema", "tema",
             "hma", "zlema", "kama", "tsi", "cmo", "uo", "ao", "fisher",
             "elder_ray", "tr", "msd", "chandelier", "squeeze", "apz"}
    cat = {i["id"] for i in R.catalog()["indicators"]}
    assert added <= cat


def test_no_volume_family_was_added():
    """#166 deliberately excludes MFI/ADL/Chaikin/EFI/...: Beacon's only volume
    input is a broker tick-count on a synthetic CFD (capital_com.py:807), so those
    would be noise dressed as signal. `obv`/`vwap` predate this and keep their
    '(needs volume)' caveat."""
    ids = {s["id"] for s in R.REGISTRY}
    forbidden = {"mfi", "adl", "chaikin", "efi", "vzo", "pzo", "emv", "vpt",
                 "vfi", "tmf", "wobv"}
    assert ids & forbidden == set()
    assert {s["id"] for s in R.REGISTRY if s["category"] == "volume"} == {"obv", "vwap"}


def test_core_imports_no_pandas_or_numpy():
    """CLAUDE.md §6: core is pip-installed into EVERY python image. FinTA needs
    pandas, so the temptation to reach for it is real — this pins the whole package
    (not just the TA module) to zero scientific-stack dependencies."""
    import ast
    import pathlib

    import beacon_core

    root = pathlib.Path(beacon_core.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(n.split(".")[0] in ("pandas", "numpy", "finta") for n in names):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []
