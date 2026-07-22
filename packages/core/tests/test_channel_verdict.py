"""Weekly channel verdict roll-up (#117): the keep/watch/cut synthesis behind
/analytics/synthesis. Pure math (reuses `posterior`), tested DB-free — the
decision layer must never over-state significance the sample doesn't support."""
from beacon_core.analysis.report import (channel_verdict_rollup, SIGNIFICANCE_N,
                                         _channel_verdict_query)


def test_verdict_query_compiles():
    """Regression (#117): the join must compile. The select carries no
    SignalAnalytics column, so without an explicit select_from SQLAlchemy raises
    'Can't determine which FROM clause to join from' at compile time — a 500 the
    unit-testable rollup can't catch. Compiling the real query guards it."""
    sql = str(_channel_verdict_query().compile()).lower()
    assert "signal_analytics" in sql and "join" in sql


def _rows(channel, wins, losses):
    return ([{"channel": channel, "realized_pl": 1.0}] * wins
            + [{"channel": channel, "realized_pl": -1.0}] * losses)


def _chan(rep, name):
    return next(c for c in rep["channels"] if c["channel"] == name)


def test_empty():
    rep = channel_verdict_rollup([])
    assert rep["channels"] == []
    assert rep["n_labelled"] == 0
    assert rep["any_credible_edge"] is False
    assert rep["base_rate"] == 0.5


def test_significance_states_track_sample_size():
    """Below watch_n -> gathering; watch_n..sig-1 -> watch; >=sig -> significant."""
    sig = SIGNIFICANCE_N                       # 30
    watch = (sig + 1) // 2                      # 15
    rows = (_rows("small", 3, 2)               # n=5  -> gathering
            + _rows("mid", watch, 0)           # n=15 -> watch
            + _rows("big", sig, sig))          # n=60 -> significant
    rep = channel_verdict_rollup(rows)
    assert _chan(rep, "small")["state"] == "gathering"
    assert _chan(rep, "mid")["state"] == "watch"
    assert _chan(rep, "big")["state"] == "significant"


def test_watch_and_gathering_never_read_as_a_verdict():
    """A thin channel — even at 100% raw — must not surface keep/cut. This is the
    'implies certainty it doesn't have' failure #117 targets (the 78% on ~9)."""
    rep = channel_verdict_rollup(_rows("hot", 9, 0))     # 9/9 = 100% raw, tiny n
    c = _chan(rep, "hot")
    assert c["state"] == "gathering"
    assert c["verdict"] == "gathering"
    assert rep["any_credible_edge"] is False             # nothing credible yet


def test_significant_keep_when_lower_bound_beats_base():
    """A large, clearly-winning channel against a losing field crosses to keep and
    flips any_credible_edge on."""
    # Field: two channels that lose heavily to drag the base rate well below the
    # winner, so the winner's 90% lower bound clears the base.
    rows = (_rows("winner", 55, 5)            # n=60, ~92% -> ci_low well above base
            + _rows("loserA", 5, 55)          # drags base down
            + _rows("loserB", 5, 55))
    rep = channel_verdict_rollup(rows)
    w = _chan(rep, "winner")
    assert w["state"] == "significant"
    assert w["verdict"] == "keep"
    assert w["ci_low"] > rep["base_rate"]
    assert rep["any_credible_edge"] is True
    assert rep["n_significant"] >= 1


def test_significant_cut_when_upper_bound_below_base():
    rows = (_rows("winnerA", 55, 5) + _rows("winnerB", 55, 5)
            + _rows("loser", 5, 55))          # n=60, ~8% -> ci_high below base
    rep = channel_verdict_rollup(rows)
    l = _chan(rep, "loser")
    assert l["state"] == "significant"
    assert l["verdict"] == "cut"
    assert l["ci_high"] < rep["base_rate"]


def test_significant_hold_when_interval_straddles_base():
    """A large channel that performs like the field -> significant but 'hold', not
    a keep/cut — it has enough N but no separation."""
    rows = _rows("a", 30, 30) + _rows("b", 30, 30)       # everyone at base
    rep = channel_verdict_rollup(rows)
    a = _chan(rep, "a")
    assert a["state"] == "significant"
    assert a["verdict"] == "hold"
    assert rep["any_credible_edge"] is False


def test_significant_channels_sort_first():
    rows = (_rows("thin", 4, 1)               # gathering
            + _rows("solid", 40, 20))          # significant
    rep = channel_verdict_rollup(rows)
    assert rep["channels"][0]["channel"] == "solid"       # significant ranks above thin


def test_none_pnl_and_missing_channel_handled():
    rows = [{"channel": None, "realized_pl": 1.0},
            {"channel": "x", "realized_pl": None},        # unclosed -> skipped
            {"channel": "x", "realized_pl": 2.0}]
    rep = channel_verdict_rollup(rows)
    assert rep["n_labelled"] == 2                          # the None-pnl row skipped
    assert _chan(rep, "Unattributed")["n"] == 1            # None channel bucketed
    assert _chan(rep, "x")["n"] == 1


def test_custom_significance_floor():
    rep = channel_verdict_rollup(_rows("c", 6, 4), significance_n=10)
    assert rep["significance_n"] == 10 and rep["watch_n"] == 5
    assert _chan(rep, "c")["state"] == "significant"       # n=10 hits the custom floor
