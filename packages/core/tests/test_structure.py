"""Market-structure TA indicators — Fair Value Gap + Order Block (#59). Pure
detector math + registry wiring; runs on a bare box."""
from beacon_core.ta import indicators as I
from beacon_core.ta import registry as R


def _ramp(n=20, base=100.0, step=0.1):
    highs = [base + 0.5 + i * step for i in range(n)]
    lows = [base - 0.5 + i * step for i in range(n)]
    closes = [base + i * step for i in range(n)]
    return highs, lows, closes


def test_fvg_detects_bullish_gap():
    highs, lows, closes = _ramp()
    # inject a bullish imbalance at t=18: low[18] > high[16]
    lows[18] = highs[16] + 2.0
    highs[18] = lows[18] + 0.5
    closes[18] = lows[18] + 0.2
    r = I.fair_value_gap(highs, lows, closes, closes[-1], min_gap_atr=0.0, lookback=50)
    assert r["direction"] == "bull"
    assert r["bottom"] == highs[16] and r["top"] == lows[18]
    assert r["size_pct"] > 0 and r["dist_pct"] is not None


def test_fvg_none_and_absent():
    assert I.fair_value_gap([1, 2], [1, 1], [1, 1], 1, 0, 50) is None      # too few bars
    highs, lows, closes = _ramp(step=0.0)                                  # flat -> no gaps
    r = I.fair_value_gap(highs, lows, closes, 100.0, 0.0, 50)
    assert r["present"] is False and r["direction"] is None


def test_fvg_min_gap_atr_filters_noise():
    highs, lows, closes = _ramp()
    lows[18] = highs[16] + 0.05        # a tiny gap
    highs[18] = lows[18] + 0.1
    # a large min_gap_atr threshold rejects the tiny gap
    r = I.fair_value_gap(highs, lows, closes, closes[-1], min_gap_atr=5.0, lookback=50)
    assert r["present"] is False


def test_order_block_detects_bullish_zone():
    highs, lows, closes = _ramp(step=0.05)
    opens = [c - 0.02 for c in closes]
    # bearish candle at 17, strong bullish impulse at 18
    opens[17], closes[17] = 110.0, 108.0
    highs[17], lows[17] = 110.5, 107.5
    opens[18], closes[18] = 108.0, 118.0
    highs[18], lows[18] = 118.5, 107.9
    ob = I.order_block(opens, highs, lows, closes, closes[-1], disp_atr=0.5, lookback=50)
    assert ob["type"] == "bull"
    assert ob["top"] == highs[17] and ob["bottom"] == lows[17]
    assert ob["mitigated"] is False and ob["present"] is True


def test_order_block_needs_opens():
    highs, lows, closes = _ramp()
    assert I.order_block(None, highs, lows, closes, 100.0, 1.0, 50) is None
    assert I.order_block([], highs, lows, closes, 100.0, 1.0, 50) is None


def test_registry_exposes_structure_indicators():
    cat = R.catalog()
    ids = {i["id"]: i for i in cat["indicators"]}
    assert "fvg" in ids and ids["fvg"]["category"] == "structure"
    assert "order_block" in ids and ids["order_block"]["category"] == "structure"
    # both in the default config so they're captured out of the box
    default_ids = {c["id"] for c in R.DEFAULT_CONFIG["indicators"]}
    assert {"fvg", "order_block"} <= default_ids


def test_compute_one_dispatches_structure():
    highs, lows, closes = _ramp(step=0.05)
    opens = [c - 0.02 for c in closes]
    ctx = R.Ctx(closes=closes, highs=highs, lows=lows, volumes=[None] * len(closes),
                price=closes[-1], opens=opens)
    # instance_key embeds the param values (e.g. "fvg_0_50"), so match by prefix
    key, out = R.compute_one(ctx, {"id": "fvg", "params": {"min_gap_atr": 0, "lookback": 50}})
    assert key.startswith("fvg") and "present" in out
    key2, out2 = R.compute_one(ctx, {"id": "order_block", "params": {"disp_atr": 0.5, "lookback": 50}})
    assert key2.startswith("order_block") and "present" in out2


def test_structure_membership_from_features():
    from beacon_core.analysis.report import _structure_membership
    # inside an unfilled FVG (present + dist_pct 0); OB present but away
    f = {"1h": {"fvg_0.25_50": {"present": True, "dist_pct": 0},
                "order_block_1_50": {"present": True, "dist_pct": 3.2}}}
    assert _structure_membership(f) == (True, False)
    # inside an unmitigated OB on another tf
    assert _structure_membership({"4h": {"order_block_1_50": {"present": True, "dist_pct": 0}}}) == (False, True)
    # filled/away -> neither
    f3 = {"1h": {"fvg_0.25_50": {"present": False, "dist_pct": 0},
                 "order_block_1_50": {"present": True, "dist_pct": 1.1}}}
    assert _structure_membership(f3) == (False, False)
    assert _structure_membership({}) == (False, False)
    assert _structure_membership(None) == (False, False)


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok ", n)
    print("ALL PASS")
