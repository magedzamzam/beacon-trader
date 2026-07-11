"""Trend-alignment entry filter (#48): alignment logic + skip/desize decisions.
Pure (no DB/broker) — the executor wires the 4h EMA fetch around this."""
from beacon_core.execution.trend_filter import (DEFAULT_TREND_FILTER,
                                                trend_filter_cfg, is_aligned, decide)


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


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok ", n)
    print("ALL PASS")
