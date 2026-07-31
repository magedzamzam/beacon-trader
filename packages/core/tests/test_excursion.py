"""Exit-independent excursion reconstruction (#182). Pure — no DB, no candles on
disk; the bars are hand-built so each case pins one semantic decision."""
from beacon_core.analysis.excursion import (LADDER, RACE_HORIZON, RACE_SL,
                                            RACE_TP1, excursion, excursion_label)


def bar(hb, lb, ha=None, la=None):
    """One 1m bar. Ask defaults to bid + 0.30 (a realistic gold spread), so a
    BUY case can be written in bid terms and a SELL case in ask terms."""
    return {"high_bid": hb, "low_bid": lb,
            "high_ask": ha if ha is not None else hb + 0.30,
            "low_ask": la if la is not None else lb + 0.30}


# entry 100, SL 90 -> R = 10, TP1 105 -> 0.5R
BUY = dict(direction="BUY", entry=100.0, sl=90.0, tp1=105.0)


def test_buy_measures_favourable_travel_on_the_bid():
    """A BUY exits by SELLING, so its favourable excursion is a BID high — using
    the ask would credit the trade with a price it could never have sold at."""
    r = excursion([bar(120.0, 99.0, ha=125.0, la=99.3)], **BUY)
    assert r["r"] == 10.0
    assert r["mfe_r"] == 2.0                  # (120 - 100) / 10, NOT (125 - 100)
    assert r["mae_r"] == 0.1                  # (100 - 99) / 10
    assert r["tp1_r"] == 0.5                  # TP1 sits half an R away


def test_sell_measures_favourable_travel_on_the_ask():
    """The mirror: a SELL exits by BUYING, so favourable is a low ASK."""
    r = excursion([bar(101.0, 79.7, ha=101.3, la=80.0)],
                  direction="SELL", entry=100.0, sl=110.0, tp1=95.0)
    assert r["mfe_r"] == 2.0                  # (100 - 80) / 10 off the ask
    assert round(r["mae_r"], 6) == 0.13       # (101.3 - 100) / 10, ask side
    assert r["race"] == RACE_TP1


def test_mfe_stops_at_the_original_stop():
    """The excursion window CLOSES at the stop — travel after it is not something
    the signal offered, and counting it would invent an edge the trade never had."""
    bars = [bar(103.0, 99.0),      # +0.3R
            bar(101.0, 89.0),      # stop taken here
            bar(200.0, 150.0)]     # a moonshot AFTER the stop: must not count
    r = excursion(bars, **BUY)
    assert r["mfe_r"] == 0.3
    assert r["race"] == RACE_SL and r["bars_to_sl"] == 1
    assert r["n_bars"] == 3


def test_same_bar_tp_and_sl_is_scored_as_the_stop_and_flagged():
    """A 1m bar touching both cannot say which came first. Conservative: the stop
    wins, the bar does not extend MFE, and the ambiguity is COUNTED — it is a
    headline caveat on the ladder, not a footnote."""
    r = excursion([bar(106.0, 89.0)], **BUY)
    assert r["same_bar_ambiguous"] is True
    assert r["race"] == RACE_SL
    assert r["bars_to_tp1"] is None
    assert r["mfe_r"] == 0.0                  # the straddling bar is not credited
    assert r["ladder"]["0.25"] is False


def test_ladder_is_reached_x_r_before_the_stop():
    bars = [bar(101.0, 99.5), bar(115.0, 100.0), bar(112.0, 89.0)]
    r = excursion(bars, **BUY)
    assert r["mfe_r"] == 1.5                  # (115 - 100) / 10
    assert [k for k, v in r["ladder"].items() if v] == ["0.25", "0.5", "1.0", "1.5"]
    assert r["ladder"]["2.0"] is False
    assert set(r["ladder"]) == {"0.25", "0.5", "1.0", "1.5", "2.0", "3.0"}


def test_mae_is_adverse_travel_before_tp1_only():
    """MAE answers "how much heat before it worked", so it closes at TP1. Drawdown
    after TP1 is the exit's problem, not the signal's."""
    bars = [bar(101.0, 96.0),      # -0.4R of heat
            bar(106.0, 99.0),      # TP1 reached here
            bar(107.0, 91.0)]      # later heat: after TP1, excluded
    r = excursion(bars, **BUY)
    assert r["mae_r"] == 0.4
    assert r["race"] == RACE_TP1 and r["bars_to_tp1"] == 1


def test_unresolved_signal_terminates_at_the_horizon():
    """A signal that hits neither must still end — and say so, so the caller can
    report how many never resolved instead of silently calling them losses."""
    bars = [bar(101.0, 99.0)] * 50
    r = excursion(bars, horizon_bars=10, **BUY)
    assert r["n_bars"] == 10
    assert r["race"] == RACE_HORIZON
    assert r["resolved"] is False and r["horizon_capped"] is True


def test_undefined_geometry_returns_none():
    assert excursion([], **BUY) is None
    assert excursion([bar(101.0, 99.0)], direction="BUY", entry=100.0, sl=100.0) is None


def test_tp1_on_the_wrong_side_is_ignored_not_counted_as_easy():
    """A BUY with a TP below entry is bad data. Reporting -0.5R would read as a
    trivially reachable target and quietly inflate the channel's ladder."""
    r = excursion([bar(101.0, 99.0)], direction="BUY", entry=100.0, sl=90.0, tp1=95.0)
    assert r["tp1_r"] is None
    assert r["race"] == RACE_HORIZON          # no TP1 to race against


def test_label_is_did_the_market_offer_the_R():
    assert excursion_label({"mfe_r": 1.4}) is True
    assert excursion_label({"mfe_r": 0.9}) is False
    assert excursion_label({"mfe_r": 0.6}, min_r=0.5) is True
    assert excursion_label(None) is None       # no reconstruction -> excluded
    assert excursion_label({"mfe_r": None}) is None


def test_ladder_default_rungs_are_stable_keys():
    r = excursion([bar(140.0, 99.0)], **BUY)
    assert list(r["ladder"]) == [f"{x:g}" if x % 1 else f"{x:.1f}" for x in LADDER]
