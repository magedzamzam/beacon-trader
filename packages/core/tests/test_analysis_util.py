"""Shared analytics helpers (#69) — pure, bare-box unit tests."""
from beacon_core.analysis._util import (bars_col, dig, dig_num, overlay_config,
                                        adverse_side, zone_side)


def test_bars_col_skips_missing():
    bars = [{"c": 1, "h": 2}, {"h": 3}, {"c": 4}]
    assert bars_col(bars, "c") == [1.0, 4.0]
    assert bars_col(bars, "h") == [2.0, 3.0]
    assert bars_col(None, "c") == [] and bars_col([], "c") == []


def test_dig_walks_and_bails():
    d = {"a": {"b": {"c": 5}}}
    assert dig(d, "a", "b", "c") == 5
    assert dig(d, "a", "x") is None            # missing key
    assert dig(d, "a", "b", "c", "d") is None  # walked past a scalar
    assert dig({}, "a") is None and dig(None, "a") is None
    assert dig({"a": "text"}, "a") == "text"   # dig returns ANY value


def test_dig_num_numeric_only():
    d = {"a": {"n": 3.5, "s": "x", "b": True}}
    assert dig_num(d, "a", "n") == 3.5
    assert dig_num(d, "a", "s") is None         # non-numeric -> None
    assert dig_num(d, "a", "b") is True         # bool passes (int subclass) — matches originals
    assert dig_num(d, "missing") is None


def test_overlay_config_only_known_keys():
    defaults = {"x": 1, "y": 2}
    assert overlay_config(defaults, {"x": 9, "z": 99}) == {"x": 9, "y": 2}  # z ignored, y default
    assert overlay_config(defaults, None) == {"x": 1, "y": 2}
    out = overlay_config(defaults, {})
    out["x"] = 5
    assert defaults["x"] == 1                   # returns a copy, doesn't mutate defaults


def test_adverse_side():
    assert adverse_side("BUY", "above") is True     # buying into resistance
    assert adverse_side("SELL", "below") is True    # selling into support
    assert adverse_side("BUY", "below") is False
    assert adverse_side("SELL", "above") is False
    assert adverse_side("BUY", None) is False


def test_zone_side():
    assert zone_side(100, 90, 110) == "inside"
    assert zone_side(120, 90, 110) == "above"
    assert zone_side(80, 90, 110) == "below"


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok ", n)
    print("ALL PASS")
