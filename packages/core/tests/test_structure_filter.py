"""Structure-map Phase-3 filter scaffolding (#61) + HTF-alignment helper. Pure —
the filter is SHADOW (not wired to the executor); this just verifies the decision
logic and config overlay so Phase 3 can flip it on after the edge is measured."""
from beacon_core.analysis.structure_filter import (DEFAULT_STRUCTURE_FILTER,
                                                   structure_filter_cfg, decide)
from beacon_core.analysis.estimators import _htf_alignment


class _S:
    def __init__(self, label):
        self.label = label


def test_default_filter_is_off_and_fail_open():
    cfg = structure_filter_cfg(None)
    assert cfg["enabled"] is False
    sm = {"nearest_zone": {"side": "above", "dist_atr": 0.1}, "htf_alignment": "counter"}
    assert decide(cfg, "BUY", sm) == ("allow", 1.0, None)          # disabled -> allow
    on = structure_filter_cfg({"filter": {"enabled": True}})
    assert decide(on, "BUY", None) == ("allow", 1.0, None)         # no context -> allow


def test_skip_on_adverse_magnet():
    cfg = structure_filter_cfg({"filter": {"enabled": True, "mode": "skip",
                                           "adverse_zone_atr": 0.5}})
    # BUY into a zone just above (resistance) within range -> skip
    sm = {"nearest_zone": {"side": "above", "dist_atr": 0.3}, "htf_alignment": "aligned"}
    assert decide(cfg, "BUY", sm) == ("skip", 0.0, "adverse_magnet")
    # SELL into a zone below (support) -> skip
    sm2 = {"nearest_zone": {"side": "below", "dist_atr": 0.2}, "htf_alignment": "mixed"}
    assert decide(cfg, "SELL", sm2)[0] == "skip"
    # BUY with the zone below (not adverse) -> allow
    sm3 = {"nearest_zone": {"side": "below", "dist_atr": 0.2}, "htf_alignment": "aligned"}
    assert decide(cfg, "BUY", sm3) == ("allow", 1.0, None)
    # adverse but too far -> allow
    sm4 = {"nearest_zone": {"side": "above", "dist_atr": 1.5}, "htf_alignment": "aligned"}
    assert decide(cfg, "BUY", sm4) == ("allow", 1.0, None)


def test_desize_and_htf_reason():
    cfg = structure_filter_cfg({"filter": {"enabled": True, "mode": "desize",
                                           "desize_factor": 0.25,
                                           "require_htf_aligned": True}})
    sm = {"nearest_zone": {"side": "below", "dist_atr": 3.0}, "htf_alignment": "counter"}
    action, factor, reason = decide(cfg, "BUY", sm)
    assert action == "allow" and factor == 0.25 and reason == "htf_counter"


def test_htf_alignment_helper():
    aligned = {"1w": _S("bull"), "1d": _S("bull")}
    counter = {"1w": _S("bear"), "1d": _S("bear")}
    mixed = {"1w": _S("bull"), "1d": _S("bear")}
    assert _htf_alignment("BUY", aligned) == "aligned"
    assert _htf_alignment("BUY", counter) == "counter"
    assert _htf_alignment("BUY", mixed) == "mixed"
    assert _htf_alignment("SELL", counter) == "aligned"
    assert _htf_alignment(None, aligned) == "mixed"
    assert _htf_alignment("BUY", {}) == "mixed"


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok ", n)
    print("ALL PASS")
