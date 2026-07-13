"""Trend-alignment entry filter (#48): alignment logic + skip/desize decisions.
Pure (no DB/broker) — the executor wires the 4h EMA fetch around this."""
from beacon_core.execution.trend_filter import (DEFAULT_TREND_FILTER,
                                                trend_filter_cfg, is_aligned, decide,
                                                alignment_from_features)


def test_alignment_semantics():
    # BUY aligns with an up-trend (price above EMA); SELL with a down-trend.
    assert is_aligned("BUY", True) is True
    assert is_aligned("BUY", False) is False
    assert is_aligned("SELL", False) is True
    assert is_aligned("SELL", True) is False


def test_default_is_off_and_fail_open():
    cfg = trend_filter_cfg(None)
    assert cfg["enabled"] is False
    # disabled -> always allow at full size regardless of trend
    assert decide(cfg, "BUY", False) == ("allow", 1.0, None)
    # even enabled, an UNKNOWN trend (None) never blocks
    on = trend_filter_cfg({"trend_alignment": {"enabled": True}})
    assert decide(on, "SELL", None) == ("allow", 1.0, None)


def test_skip_counter_trend_when_enabled():
    cfg = trend_filter_cfg({"trend_alignment": {"enabled": True, "mode": "skip"}})
    # aligned -> allow full size
    assert decide(cfg, "SELL", False) == ("allow", 1.0, True)
    # counter-trend -> skip
    assert decide(cfg, "BUY", False) == ("skip", 0.0, False)
    assert decide(cfg, "SELL", True) == ("skip", 0.0, False)


def test_desize_counter_trend():
    cfg = trend_filter_cfg({"trend_alignment": {
        "enabled": True, "mode": "desize", "desize_factor": 0.25}})
    assert decide(cfg, "BUY", True) == ("allow", 1.0, True)      # aligned untouched
    action, factor, aligned = decide(cfg, "BUY", False)          # counter -> de-size
    assert action == "allow" and factor == 0.25 and aligned is False


def test_desize_factor_zero_falls_back_to_skip():
    cfg = trend_filter_cfg({"trend_alignment": {
        "enabled": True, "mode": "desize", "desize_factor": 0}})
    assert decide(cfg, "BUY", False) == ("skip", 0.0, False)


def test_cfg_overlays_only_known_keys():
    cfg = trend_filter_cfg({"trend_alignment": {"enabled": True, "bogus": 1,
                                                "timeframe": "1d"}})
    assert cfg["enabled"] is True and cfg["timeframe"] == "1d"
    assert "bogus" not in cfg
    assert cfg["ema_period"] == DEFAULT_TREND_FILTER["ema_period"]  # untouched default


def test_alignment_from_features():
    # #72 metric: classify a persisted signal_features snapshot by the 4h EMA200
    # `above` flag. SELL below EMA = aligned; BUY below EMA = counter.
    feats = {"4h": {"ema_200": {"value": 4200.0, "above": False}, "_price": 4180.0}}
    assert alignment_from_features(feats, "SELL") is True     # down-trend, SELL aligns
    assert alignment_from_features(feats, "BUY") is False     # down-trend, BUY counters
    up = {"4h": {"ema_200": {"value": 4100.0, "above": True}}}
    assert alignment_from_features(up, "BUY") is True
    assert alignment_from_features(up, "SELL") is False
    # honours a non-default timeframe/period the live filter may be set to
    assert alignment_from_features({"1d": {"ema_100": {"above": True}}},
                                   "BUY", timeframe="1d", ema_period=100) is True


def test_alignment_from_features_unknown_is_none():
    # fail-open: missing tf / missing EMA / missing `above` -> None (excluded).
    assert alignment_from_features(None, "BUY") is None
    assert alignment_from_features({}, "BUY") is None
    assert alignment_from_features({"4h": {}}, "BUY") is None            # EMA not captured
    assert alignment_from_features({"4h": {"ema_200": {"value": 1.0}}},  # no `above`
                                   "BUY") is None
    assert alignment_from_features({"1h": {"ema_200": {"above": True}}}, # wrong tf
                                   "BUY") is None


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok ", n)
    print("ALL PASS")
