"""Turtle exit counterfactual (#170) — would getting out on a trend flip have
beaten where the trade actually closed? Pure backtest; nothing here gates."""
import datetime as dt

from beacon_core.analysis.turtle import exit_counterfactual, _bar_time, _no_longer_backing
from beacon_core.analysis.report import turtle_exit_rollup

T0 = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)


def _bars(closes, start=T0, step_h=1):
    return [{"t": (start + dt.timedelta(hours=i * step_h)).isoformat(), "c": c}
            for i, c in enumerate(closes)]


def _at(i, start=T0, step_h=1):
    return start + dt.timedelta(hours=i * step_h)


# A 60-bar flat base (so the 55-bar channel is warm and neutral), then a rally
# that breaks the channel high -> Turtle long, then a collapse that breaks the
# low -> Turtle short. Entry sits in the rally, so the flip is inside the hold.
BASE = [100.0] * 60
RALLY = [100 + i for i in range(1, 21)]          # 101..120, breaks the 55-bar high
CRASH = [120 - 3 * i for i in range(1, 21)]      # 117..60, breaks the 55-bar low
SERIES = BASE + RALLY + CRASH


# ------------------------------------------------------------------ helpers
def test_bar_time_accepts_iso_and_datetime():
    assert _bar_time({"t": "2026-07-01T00:00:00+00:00"}) == T0
    assert _bar_time({"t": "2026-07-01T00:00:00Z"}) == T0
    assert _bar_time({"t": T0}) == T0
    assert _bar_time({"t": dt.datetime(2026, 7, 1)}) == T0      # naive -> UTC
    assert _bar_time({"t": "not a date"}) is None
    assert _bar_time({}) is None


def test_no_longer_backing_semantics():
    # reference series never prints 0, so a flip is a sign change
    assert _no_longer_backing(-1.0, "BUY", "signal") is True
    assert _no_longer_backing(1.0, "BUY", "signal") is False
    assert _no_longer_backing(0.0, "BUY", "signal") is False
    # the flat variant DOES go flat, and flat is already "not backing you"
    assert _no_longer_backing(0, "BUY", "signal_flat") is True
    assert _no_longer_backing(0, "SELL", "signal_flat") is True
    assert _no_longer_backing(1.0, "SELL", "signal") is True
    assert _no_longer_backing(None, "BUY", "signal") is False


# ------------------------------------------------------- exit_counterfactual
def test_flip_exit_beats_riding_a_collapse_when_the_stop_is_wide():
    """The case the report exists for: long into a rally, held all the way down
    to a WIDE stop. The Turtle breaks short well before price gets there, so
    exiting on the flip saves most of the loss."""
    bars = _bars(SERIES)
    cf = exit_counterfactual(
        bars=bars, entry_time=_at(70), exit_time=_at(len(SERIES) - 1),
        entry_price=110.0, sl_price=80.0, actual_exit_price=80.0,
        direction="BUY")
    assert cf["flipped"] is True
    assert cf["actual_r"] == -1.0                       # rode it to the stop
    assert cf["counterfactual_r"] > cf["actual_r"]
    assert cf["delta_r"] > 0.5
    assert 60.0 < cf["exit_price"] < 120.0
    assert cf["bars_held"] is not None and cf["bars_held"] > 0


def test_a_tight_stop_is_hit_long_before_a_55_bar_channel_breaks():
    """The structural limit on this whole idea, pinned so it cannot be forgotten:
    a 55-bar Donchian flip is a SLOW signal. When the stop sits close to the
    channel, price reaches the stop first and the flip-exit is strictly WORSE.
    A positive mean_delta_r therefore has to come from wide-stop trades — if the
    report ever shows an edge, check it is not an artifact of stop distance."""
    bars = _bars(SERIES)
    cf = exit_counterfactual(
        bars=bars, entry_time=_at(70), exit_time=_at(len(SERIES) - 1),
        entry_price=110.0, sl_price=100.0, actual_exit_price=100.0,
        direction="BUY")
    assert cf["flipped"] is True
    assert cf["counterfactual_r"] < cf["actual_r"]      # flip came too late
    assert cf["delta_r"] < 0


def test_no_flip_inside_the_hold_means_no_change():
    """If the Turtle never turned against the trade before it closed, the
    counterfactual must be identical to the actual — delta exactly 0."""
    bars = _bars(BASE + RALLY)
    cf = exit_counterfactual(
        bars=bars, entry_time=_at(62), exit_time=_at(len(BASE + RALLY) - 1),
        entry_price=103.0, sl_price=100.0, actual_exit_price=120.0,
        direction="BUY")
    assert cf["flipped"] is False
    assert cf["delta_r"] == 0.0
    assert cf["counterfactual_r"] == cf["actual_r"]
    assert cf["exit_time"] is None and cf["exit_price"] is None


def test_flip_before_entry_is_ignored():
    """Only flips INSIDE the holding period count — the series turning before we
    were in the trade is not an exit signal for it."""
    bars = _bars(SERIES)
    late_entry = _at(len(SERIES) - 3)                  # enter after the collapse
    cf = exit_counterfactual(
        bars=bars, entry_time=late_entry, exit_time=_at(len(SERIES) - 1),
        entry_price=66.0, sl_price=60.0, actual_exit_price=62.0, direction="SELL")
    # Turtle is already short here, which BACKS a SELL — so no flip against it.
    assert cf["flipped"] is False


def test_short_side_is_mirrored():
    """A SELL held through the rally: the Turtle going long is the flip."""
    bars = _bars(SERIES)
    cf = exit_counterfactual(
        bars=bars, entry_time=_at(58), exit_time=_at(85),
        entry_price=100.0, sl_price=110.0, actual_exit_price=118.0,
        direction="SELL")
    assert cf["flipped"] is True
    assert cf["counterfactual_r"] > cf["actual_r"]      # cut before the full run


def test_r_is_price_basis_off_the_same_entry_and_risk():
    bars = _bars(SERIES)
    cf = exit_counterfactual(
        bars=bars, entry_time=_at(70), exit_time=_at(len(SERIES) - 1),
        entry_price=110.0, sl_price=100.0, actual_exit_price=115.0,
        direction="BUY")
    assert cf["actual_r"] == 0.5                        # (115-110)/10
    assert cf["counterfactual_r"] == round((cf["exit_price"] - 110.0) / 10.0, 4)


def test_flat_variant_exits_earlier_than_the_reference():
    """The reference stop-and-reverses; the flat variant leaves on the mean
    cross, so it can never exit later."""
    bars = _bars(SERIES)
    kw = dict(bars=bars, entry_time=_at(70), exit_time=_at(len(SERIES) - 1),
              entry_price=110.0, sl_price=100.0, actual_exit_price=100.0,
              direction="BUY")
    ref = exit_counterfactual(**kw, variant="signal")
    flat = exit_counterfactual(**kw, variant="signal_flat")
    assert flat["flipped"] and ref["flipped"]
    assert flat["bars_held"] <= ref["bars_held"]


def test_unusable_inputs_return_none():
    bars = _bars(SERIES)
    common = dict(entry_time=_at(70), exit_time=_at(90), entry_price=110.0,
                  actual_exit_price=100.0, direction="BUY")
    assert exit_counterfactual(bars=bars, sl_price=110.0, **common) is None   # zero risk
    assert exit_counterfactual(bars=[], sl_price=100.0, **common) is None
    assert exit_counterfactual(bars=_bars([1.0] * 10), sl_price=100.0, **common) is None
    assert exit_counterfactual(bars=bars, sl_price=None, **common) is None
    assert exit_counterfactual(bars=bars, sl_price=100.0,
                               **{**common, "entry_time": None}) is None


def test_bars_without_a_usable_timestamp_are_dropped():
    bars = _bars(SERIES)
    bars.insert(30, {"t": "garbage", "c": 999.0})
    bars.insert(31, {"c": 999.0})
    cf = exit_counterfactual(
        bars=bars, entry_time=_at(70), exit_time=_at(len(SERIES) - 1),
        entry_price=110.0, sl_price=100.0, actual_exit_price=100.0, direction="BUY")
    assert cf is not None and cf["n_bars"] == len(SERIES)


# ------------------------------------------- mechanism split (#171)
def test_a_trade_the_turtle_already_opposed_is_not_a_flip():
    """#171: entering a BUY while the Donchian is already short is not a
    trend-exit signal — it is an ENTRY FILTER signal. The first version counted
    it as a 'flip' exiting one bar after entry, which blended two mechanisms
    with very different costs to act on."""
    bars = _bars(SERIES)
    # By the end of CRASH the Turtle is short; open a BUY there.
    cf = exit_counterfactual(
        bars=bars, entry_time=_at(95), exit_time=_at(len(SERIES) - 1),
        entry_price=75.0, sl_price=70.0, actual_exit_price=70.0, direction="BUY")
    assert cf["backed_at_entry"] is False
    assert cf["mechanism"] == "opposed_at_entry"


def test_a_trade_the_turtle_backed_then_turned_is_a_real_flip():
    """The only population that could justify an exit engine."""
    bars = _bars(SERIES)
    cf = exit_counterfactual(
        bars=bars, entry_time=_at(70), exit_time=_at(len(SERIES) - 1),
        entry_price=110.0, sl_price=80.0, actual_exit_price=80.0, direction="BUY")
    assert cf["backed_at_entry"] is True
    assert cf["mechanism"] == "flipped_mid_trade"


def test_a_backed_trade_that_never_turns_has_no_mechanism():
    bars = _bars(BASE + RALLY)
    cf = exit_counterfactual(
        bars=bars, entry_time=_at(62), exit_time=_at(len(BASE + RALLY) - 1),
        entry_price=103.0, sl_price=100.0, actual_exit_price=120.0, direction="BUY")
    assert cf["backed_at_entry"] is True and cf["mechanism"] is None


def test_risk_distance_is_reported_for_the_stop_artifact_check():
    bars = _bars(SERIES)
    cf = exit_counterfactual(
        bars=bars, entry_time=_at(70), exit_time=_at(90), entry_price=110.0,
        sl_price=80.0, actual_exit_price=100.0, direction="BUY")
    assert cf["risk"] == 30.0


def test_exit_rule_block_only_counts_backed_then_turned_trades():
    rows = [
        {"backed_at_entry": True, "mechanism": "flipped_mid_trade",
         "actual_r": -1.0, "counterfactual_r": 0.0, "delta_r": 1.0},
        {"backed_at_entry": True, "mechanism": None,          # never turned
         "actual_r": 2.0, "counterfactual_r": 2.0, "delta_r": 0.0},
        {"backed_at_entry": False, "mechanism": "opposed_at_entry",
         "actual_r": -1.0, "counterfactual_r": 0.0, "delta_r": 1.0},
    ]
    out = turtle_exit_rollup(rows, significance_n=1)
    er = out["exit_rule"]
    assert er["n"] == 1                     # ONLY the backed-then-turned trade
    assert er["n_backed_at_entry"] == 2
    assert er["turn_rate"] == 0.5
    assert er["mean_delta_r"] == 1.0


def test_entry_filter_block_values_a_skip_at_zero_not_at_an_exit_price():
    """Skipping a trade means it is never taken, so its counterfactual is R = 0
    exactly — not a price one bar after an entry that never happened."""
    rows = [{"backed_at_entry": False, "mechanism": "opposed_at_entry",
             "actual_r": r, "counterfactual_r": 0.0, "delta_r": 0.0}
            for r in (-1.0, -1.0, -1.0, 1.0)]
    ef = turtle_exit_rollup(rows, significance_n=2)["entry_filter"]
    assert ef["n"] == 4
    assert ef["mean_actual_r"] == -0.5
    assert ef["mean_delta_r"] == 0.5        # not trading them adds +0.5R
    assert ef["counterfactual_r"] == 0.0
    assert ef["win_rate"] == 0.25


def test_a_mean_inside_its_own_noise_is_not_clear():
    """`clear` is the honest bar — beating zero is not enough."""
    noisy = [{"backed_at_entry": True, "mechanism": "flipped_mid_trade",
              "actual_r": 0.0, "counterfactual_r": r, "delta_r": r}
             for r in (5.0, -4.0, 4.0, -3.0, 3.0, -4.0)]
    out = turtle_exit_rollup(noisy, significance_n=2)["exit_rule"]
    assert out["mean_delta_r"] > 0 and out["clear"] is False
    tight = [{"backed_at_entry": True, "mechanism": "flipped_mid_trade",
              "actual_r": 0.0, "counterfactual_r": 1.0, "delta_r": 1.0}] * 6
    assert turtle_exit_rollup(tight, significance_n=2)["exit_rule"]["clear"] is True


def test_stop_distance_tertiles_expose_a_wide_stop_artifact():
    """The artifact #170 warned about: if the whole delta sits in the widest
    tertile it is a finding about stop placement, not about the Turtle."""
    rows = ([{"risk": 1.0 + i, "delta_r": 0.0, "backed_at_entry": True,
              "mechanism": "flipped_mid_trade", "actual_r": 0.0,
              "counterfactual_r": 0.0} for i in range(6)]
            + [{"risk": 50.0 + i, "delta_r": 2.0, "backed_at_entry": True,
                "mechanism": "flipped_mid_trade", "actual_r": 0.0,
                "counterfactual_r": 2.0} for i in range(3)])
    bands = turtle_exit_rollup(rows)["by_stop_distance"]
    assert bands["narrow"]["mean_delta_r"] == 0.0
    assert bands["wide"]["mean_delta_r"] > 0
    assert bands["wide"]["risk_lo"] >= bands["narrow"]["risk_hi"]


def test_stop_distance_block_needs_a_real_sample():
    assert turtle_exit_rollup([_row("A", 0.0, 1.0)])["by_stop_distance"] is None


# ------------------------------------------------------------------- rollup
def _row(ch, actual, cf, flipped=True, direction="BUY"):
    return {"channel": ch, "direction": direction, "actual_r": actual,
            "counterfactual_r": cf, "delta_r": round(cf - actual, 4),
            "flipped": flipped}


def test_rollup_reports_the_r_a_flip_exit_would_have_added():
    rows = [_row("A", -1.0, -0.2), _row("A", -1.0, -0.4), _row("A", 2.0, 1.0)]
    out = turtle_exit_rollup(rows, significance_n=2)
    o = out["overall"]
    assert o["n"] == 3 and o["n_flipped"] == 3 and o["flip_rate"] == 1.0
    assert o["mean_actual_r"] == 0.0
    assert o["mean_counterfactual_r"] == round((-0.2 - 0.4 + 1.0) / 3, 4)
    assert o["mean_delta_r"] == round((0.8 + 0.6 - 1.0) / 3, 4)
    assert (o["helped"], o["hurt"]) == (2, 1)
    assert o["significant"] is True


def test_rollup_splits_by_channel_and_direction():
    rows = [_row("A", -1.0, 0.0), _row("B", 1.0, 0.5, direction="SELL")]
    out = turtle_exit_rollup(rows)
    assert out["by_channel"]["A"]["mean_delta_r"] == 1.0
    assert out["by_channel"]["B"]["mean_delta_r"] == -0.5
    assert set(out["by_direction"]) == {"BUY", "SELL"}


def test_rollup_flags_a_thin_sample_and_reports_a_spread():
    out = turtle_exit_rollup([_row("A", -1.0, 1.0)] * 4, significance_n=30)
    assert out["overall"]["significant"] is False
    assert out["stderr_delta_r"] == 0.0            # identical rows -> no spread
    varied = turtle_exit_rollup([_row("A", -1.0, 1.0), _row("A", 1.0, -1.0)])
    assert varied["stderr_delta_r"] > 0


def test_rollup_ignores_rows_that_could_not_be_evaluated():
    rows = [_row("A", -1.0, 0.0),
            {"channel": "B", "actual_r": None, "counterfactual_r": None, "delta_r": None}]
    out = turtle_exit_rollup(rows)
    assert out["overall"]["n"] == 1
    assert "B" not in out["by_channel"]


def test_rollup_of_nothing_is_empty_not_a_crash():
    out = turtle_exit_rollup([])
    assert out["overall"] is None
    assert out["by_channel"] == {} and out["stderr_delta_r"] is None


def test_never_flipped_trades_still_count_in_the_denominator():
    """A flip-exit rule that rarely fires is capped by its flip rate, so
    no-flip trades must dilute the mean rather than be dropped."""
    rows = [_row("A", -1.0, 1.0, flipped=True)] + [_row("A", 0.5, 0.5, flipped=False)] * 3
    o = turtle_exit_rollup(rows)["overall"]
    assert o["n"] == 4 and o["n_flipped"] == 1 and o["flip_rate"] == 0.25
    assert o["mean_delta_r"] == 0.5               # 2.0 spread over 4 trades
