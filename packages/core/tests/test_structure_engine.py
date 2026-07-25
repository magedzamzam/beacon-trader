"""Persistent market-structure + Fib magnet engine (#61) — pure pipeline:
ZigZag swings, HH/HL/LH/LL labels, structure classify, fib ladder, clustering.
Runs on a bare box (no DB/broker)."""
from beacon_core.analysis import structure as S


def _bars(path, spread=1.0):
    return [{"h": p + spread, "l": p - spread, "c": p} for p in path]


def _rising():
    # rising with pullbacks -> higher highs + higher lows
    return _bars([100, 104, 108, 110, 106, 101, 96, 95, 100, 108, 115, 120,
                  116, 108, 101, 100, 108, 120, 128, 130])


def _falling():
    return _bars([130, 126, 122, 120, 124, 129, 133, 134, 128, 120, 112, 108,
                  112, 120, 126, 128, 120, 108, 100, 96])


def test_zigzag_alternates_and_finds_pivots():
    b = _rising()
    highs = [x["h"] for x in b]
    lows = [x["l"] for x in b]
    piv = S.zigzag(highs, lows, atr=3.0, k=1.0)
    assert len(piv) >= 4
    kinds = [p["kind"] for p in piv]
    assert all(kinds[i] != kinds[i + 1] for i in range(len(kinds) - 1))  # alternating
    assert S.zigzag([1, 2], [1, 1], atr=3.0, k=1.0) == []                # too few bars
    assert S.zigzag(highs, lows, atr=0, k=1.0) == []                     # no ATR


def test_labels_and_classify_bull_bear_range():
    b = _rising()
    piv = S.zigzag([x["h"] for x in b], [x["l"] for x in b], atr=3.0, k=1.0)
    assert S.classify_structure(S.label_swings(piv)) == "bull"
    b2 = _falling()
    piv2 = S.zigzag([x["h"] for x in b2], [x["l"] for x in b2], atr=3.0, k=1.0)
    assert S.classify_structure(S.label_swings(piv2)) == "bear"


def test_premium_discount():
    assert S.premium_discount(130, 99, 131) > 0.9      # near the high -> premium
    assert S.premium_discount(100, 99, 131) < 0.1      # near the low -> discount
    assert S.premium_discount(100, None, 131) is None
    assert S.premium_discount(100, 120, 100) is None   # degenerate range


def test_fib_ladder_retracement_and_extension():
    down = S.fib_ladder(120, 100, "down", [0.5, 0.618], [1.618])
    r05 = next(x for x in down if x["ratio"] == 0.5 and x["kind"] == "fib_retracement")
    assert abs(r05["price"] - 110) < 1e-9              # 0.5 retr of 120->100
    ext = next(x for x in down if x["kind"] == "fib_extension")
    assert ext["price"] < 100                          # extension continues down
    up = S.fib_ladder(100, 120, "up", [0.618], [1.618])
    assert next(x for x in up if x["kind"] == "fib_extension")["price"] > 120


def test_cluster_scores_confluence_and_ranks():
    lvls = [
        {"price": 110.0, "weight": 2, "timeframe": "1h", "kind": "fib_retracement", "ratio": 0.5},
        {"price": 110.3, "weight": 3, "timeframe": "4h", "kind": "swing_high", "ratio": None},
        {"price": 150.0, "weight": 1, "timeframe": "1d", "kind": "fib_extension", "ratio": 1.618},
    ]
    z = S.cluster_levels(lvls, tolerance=1.0)
    assert z[0]["score"] == 5 and z[0]["n_timeframes"] == 2 and z[0]["rank"] == 1
    assert z[1]["rank"] == 2 and z[1]["score"] == 1
    assert S.cluster_levels([], 1.0) == []
    assert S.cluster_levels(lvls, 0) == []             # no tolerance -> no zones


def test_cluster_members_carry_weight_and_sum_to_score():
    # members must persist their weight so Σ(member weights) == score is auditable (#113)
    lvls = [
        {"price": 110.0, "weight": 2.0, "timeframe": "1h", "kind": "fib_retracement", "ratio": 0.5},
        {"price": 110.3, "weight": 3.0, "timeframe": "4h", "kind": "swing_high", "ratio": None},
    ]
    z = S.cluster_levels(lvls, tolerance=1.0)[0]
    assert all("weight" in m for m in z["members"])
    assert abs(sum(m["weight"] for m in z["members"]) - z["score"]) < 1e-9


def test_width_cap_splits_chained_levels_into_multiple_zones():
    # Regression for single-linkage chaining (#113): evenly-spaced levels each within
    # `tolerance` of the next chain into ONE zone without a cap, but a max_width cap
    # must split them into several tight zones instead of one range-wide blob.
    lvls = [{"price": 100.0 + i, "weight": 1.0, "timeframe": "1h",
             "kind": "fib_retracement", "ratio": None} for i in range(21)]  # 100..120, 1pt apart
    # No cap: single-linkage welds the whole 20-pt span into one mega-zone.
    uncapped = S.cluster_levels(lvls, tolerance=2.0)
    assert len(uncapped) == 1
    assert uncapped[0]["price_high"] - uncapped[0]["price_low"] == 20.0
    # With a 5-pt width cap: no zone may exceed 5 pts, so it splits into several.
    capped = S.cluster_levels(lvls, tolerance=2.0, max_width=5.0)
    assert len(capped) > 1
    assert all(z["price_high"] - z["price_low"] <= 5.0 for z in capped)


def test_analyze_timeframe_end_to_end():
    r = S.analyze_timeframe(_rising(), atr=3.0, k=1.0,
                            retr_ratios=[0.618], ext_ratios=[1.618])
    assert r["label"] == "bull"
    assert 0.0 <= r["premium_discount"] <= 1.0
    kinds = {lv["kind"] for lv in r["levels"]}
    assert "fib_retracement" in kinds and "fib_extension" in kinds
    assert {"swing_high", "swing_low"} & kinds
    # insufficient data -> None
    assert S.analyze_timeframe(_bars([1, 2, 3]), atr=1.0, k=1.0,
                               retr_ratios=[0.5], ext_ratios=[1.618]) is None


def test_config_overlay_and_contract():
    cfg = S.structure_cfg({"cluster_atr": 0.9, "bogus": 1})
    assert cfg["cluster_atr"] == 0.9 and "bogus" not in cfg
    assert cfg["timeframes"] == S.DEFAULT_STRUCTURE["timeframes"]
    fc = S.feature_contribution("magnet_proximity", 0.4, "down", 3.0, 0.7)
    assert set(fc) == {"name", "value", "direction", "weight", "confidence"}


def test_scheduled_recompute_due_daily_anchored():
    # #115: daily cadence, anchored to the UTC day boundary (not "24h since run").
    from datetime import datetime, timezone
    utc = timezone.utc
    assert S.scheduled_recompute_due(None, datetime(2026, 7, 22, 3, 0, tzinfo=utc), 1) is True
    # same UTC day as last run -> not due, even 23h later
    last = datetime(2026, 7, 22, 1, 0, tzinfo=utc)
    assert S.scheduled_recompute_due(last, datetime(2026, 7, 22, 23, 59, tzinfo=utc), 1) is False
    # a new UTC day has started -> due (fires at the boundary, doesn't drift)
    assert S.scheduled_recompute_due(last, datetime(2026, 7, 23, 0, 30, tzinfo=utc), 1) is True
    # cadence 7 -> needs 7 calendar days
    assert S.scheduled_recompute_due(last, datetime(2026, 7, 28, 12, 0, tzinfo=utc), 7) is False
    assert S.scheduled_recompute_due(last, datetime(2026, 7, 29, 0, 1, tzinfo=utc), 7) is True


def test_range_break_fires_beyond_buffer_only():
    # #115: break must exceed the range edge by buffer_atr*ATR (noise guard).
    # range 100..120, ATR 10, buffer 0.25 -> need > 2.5 pts beyond an edge.
    assert S.range_break(121.0, 100, 120, 10, 0.25) is None    # inside buffer -> no break
    assert S.range_break(123.0, 100, 120, 10, 0.25) == "up"    # clears the upper buffer
    assert S.range_break(97.0, 100, 120, 10, 0.25) == "down"   # clears the lower buffer
    assert S.range_break(110.0, 100, 120, 10, 0.25) is None    # mid-range
    # can't judge without a valid ATR / price
    assert S.range_break(200.0, 100, 120, 0, 0.25) is None
    assert S.range_break(None, 100, 120, 10, 0.25) is None


# ---- FVG magnet levels + per-kind confluence zones (#137) --------------------
def test_find_fvgs_detects_gaps_and_fill_state():
    # Build a clean bullish gap: bar t-2 high well below bar t low (3-candle imbalance).
    # highs/lows indexed together; gap between high[t-2] and low[t].
    highs = [10, 11, 12, 20, 21]
    lows =  [8,  9, 10, 18, 19]           # low[3]=18 > high[1]=11 -> bullish FVG (bottom 11, top 18)
    gaps = S.find_fvgs(highs, lows, atr=1.0, min_gap_atr=0.25, lookback=50)
    assert gaps and any(g["direction"] == "bull" for g in gaps)
    g = next(g for g in gaps if g["direction"] == "bull")
    assert g["kind"] == "fvg" and g["bottom"] == 11 and g["top"] == 18
    assert g["price"] == (11 + 18) / 2.0 and g["filled"] is False
    # A later candle trading back into [11,18] marks it filled.
    highs2 = highs + [15]
    lows2 = lows + [12]
    g2 = next(g for g in S.find_fvgs(highs2, lows2, atr=1.0) if g["direction"] == "bull")
    assert g2["filled"] is True
    # noise guard + bad input
    assert S.find_fvgs(highs, lows, atr=1.0, min_gap_atr=100) == []
    assert S.find_fvgs([1, 2], [1, 1], atr=1.0) == []


def test_strength_bucket():
    cfg = {"high": 5.0, "med": 2.5}
    assert S.strength_bucket(6.0, cfg) == "HIGH"
    assert S.strength_bucket(3.0, cfg) == "MED"
    assert S.strength_bucket(1.0, cfg) == "LOW"


def test_side_aware_kind_zones_splits_and_signs_distance():
    # price=100. Levels below (discount/buy-side) and above (premium/sell-side).
    levels = [
        {"level_id": 1, "price": 95.0, "weight": 6.0, "timeframe": "1h", "kind": "fvg", "filled": False},
        {"level_id": 2, "price": 90.0, "weight": 1.0, "timeframe": "4h", "kind": "fvg", "filled": True},
        {"level_id": 3, "price": 105.0, "weight": 3.0, "timeframe": "1h", "kind": "fvg", "filled": False},
        {"level_id": 4, "price": 112.0, "weight": 1.0, "timeframe": "1d", "kind": "fvg", "filled": False},
    ]
    cfg = {"cluster_atr": 0.5, "max_zone_width_atr": 1.0, "zone_strength": {"high": 5.0, "med": 2.5}}
    out = S.side_aware_kind_zones(levels, price=100.0, ref_atr=2.0, cfg=cfg,
                                  per_tf_atr={"1h": 2.0, "4h": 4.0, "1d": 8.0},
                                  timeframes=["1h", "4h", "1d"])
    # buy-side = below price, nearest first; distance is signed + (below).
    assert out["buy_side"] and out["buy_side"][0]["mid"] == 95.0
    assert out["buy_side"][0]["distance"] > 0 and out["buy_side"][0]["strength"] == "HIGH"
    assert out["buy_side"][0]["status"] == "Open"
    # the fully-filled 90.0 zone reports Filled
    filled_zone = next(z for z in out["buy_side"] if z["mid"] == 90.0)
    assert filled_zone["status"] == "Filled"
    # sell-side = above price, distance signed − (above)
    assert out["sell_side"] and out["sell_side"][0]["mid"] == 105.0
    assert out["sell_side"][0]["distance"] < 0
    # per-tf breakdown present for each contributing tf
    assert set(out["per_tf"]) == {"1h", "4h", "1d"}
    assert out["per_tf"]["1h"]["buy_side"][0]["mid"] == 95.0


def test_side_aware_kind_zones_caps_nearest_three():
    levels = [{"level_id": i, "price": 100.0 - i, "weight": 1.0, "timeframe": "1h",
               "kind": "fvg"} for i in range(1, 8)]     # 7 buy-side levels
    out = S.side_aware_kind_zones(levels, price=100.0, ref_atr=2.0,
                                  cfg={"cluster_atr": 0.1, "max_zone_width_atr": 0.1},
                                  per_tf_atr={"1h": 2.0}, timeframes=["1h"], top_n=3)
    assert len(out["buy_side"]) == 3 and not out["sell_side"]


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok ", n)
    print("ALL PASS")
