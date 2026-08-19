"""The engine measured against the channels it has to beat (#239)."""
from beacon_core.analysis.report import shadow_engine_rollup


def _row(kind, mfe, mae=-0.5, race="tp1", channel=None):
    return {"channel": channel or ("BeaconSharpe" if kind == "engine" else "Ch"),
            "kind": kind, "mfe_r": mfe, "mae_r": mae, "race": race,
            "tp1_r": 1.0, "ladder": {}, "horizon_capped": False}


def test_the_engine_is_pooled_against_ALL_channels_not_the_best_one():
    """A per-channel table invites picking whichever channel makes the engine
    look good. The question is whether generating beats buying, so the
    comparison is against the pool."""
    rows = ([_row("engine", 1.5)] * 3
            + [_row("telegram", 0.2, channel="A")] * 5
            + [_row("telegram", 3.0, channel="B")] * 5)
    out = shadow_engine_rollup(rows)
    assert out["channels_pooled"]["n"] == 10
    assert set(out["engines"]) == {"BeaconSharpe"}


def test_it_refuses_to_rule_before_N():
    rows = [_row("engine", 1.2)] * 5 + [_row("telegram", 0.5)] * 40
    out = shadow_engine_rollup(rows, min_n=30)
    assert out["ready"] is False
    assert "too early" in out["engines"]["BeaconSharpe"]["verdict"]


def test_it_rules_once_N_is_there():
    rows = [_row("engine", 1.2)] * 30 + [_row("telegram", 0.5)] * 40
    out = shadow_engine_rollup(rows, min_n=30)
    assert out["ready"] is True
    assert out["engines"]["BeaconSharpe"]["verdict"] == "N reached; rule on it"


def test_the_comparison_is_a_difference_not_two_numbers_to_eyeball():
    rows = [_row("engine", 1.5)] * 10 + [_row("telegram", 0.5)] * 10
    e = shadow_engine_rollup(rows)["engines"]["BeaconSharpe"]
    assert e["reach_1r"] == 1.0
    assert e["reach_1r_vs_channels"] == 1.0        # channels reach 1R never
    assert e["median_mfe_vs_channels"] == 1.0


def test_an_empty_engine_does_not_claim_a_verdict():
    out = shadow_engine_rollup([_row("telegram", 0.5)] * 10)
    assert out["engines"] == {} and out["ready"] is False
    assert out["channels_pooled"]["n"] == 10


def test_a_signal_with_no_excursion_is_not_counted_as_a_zero():
    rows = [_row("engine", None), _row("engine", 2.0)]
    out = shadow_engine_rollup(rows)
    assert out["engines"]["BeaconSharpe"]["n"] == 1
