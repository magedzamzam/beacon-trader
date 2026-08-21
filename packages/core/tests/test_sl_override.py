"""The per-channel stop override (#249): it must move the stop, size against the
NEW stop, stay placeable on every leg of a zone fanout, and never leak across the
accounts a signal is fanned onto.

The last two are the ones with money behind them. `entry_from` anchoring silently
drops the far-edge legs of a zone signal, and a shared ParsedSignal mutated in
place puts one arm's stop on every other arm.
"""
from decimal import Decimal

import pytest

from beacon_core.execution import sl_override as SLO
from beacon_core.execution import strategy as ST
from beacon_core.execution.planner import build_plan
from beacon_core.parsing.models import ParsedSignal
from beacon_core.risk.sizing import InstrumentSpec, RiskConfig, size_legs

INSTR = InstrumentSpec(value_per_point=Decimal("1"), min_lot=Decimal("0.01"),
                       lot_step=Decimal("0.01"))


def _buy(entry_from="4180", entry_to="4180", sl="4168", tps=("4190", "4200")):
    return ParsedSignal(symbol="XAUUSD", direction="BUY",
                        entry_from=Decimal(entry_from), entry_to=Decimal(entry_to),
                        sl=Decimal(sl), tps=[Decimal(t) for t in tps])


def _sell(entry_from="4180", entry_to="4180", sl="4192", tps=("4170", "4160")):
    return ParsedSignal(symbol="XAUUSD", direction="SELL",
                        entry_from=Decimal(entry_from), entry_to=Decimal(entry_to),
                        sl=Decimal(sl), tps=[Decimal(t) for t in tps])


# --- reading the setting -----------------------------------------------------

def test_an_unset_or_cleared_distance_means_the_channel_stop():
    """Empty is OFF, never a stop of zero distance. The operator clears the field
    by sending "", which is not None and would otherwise slip through."""
    for cfg in (None, {}, {"sl_distance": None}, {"sl_distance": ""},
                {"sl_distance": 0}, {"sl_distance": -3}, {"sl_distance": "nonsense"}):
        assert SLO.resolve_distance(cfg) is None


def test_a_set_distance_is_read_as_a_decimal():
    assert SLO.resolve_distance({"sl_distance": 3}) == Decimal("3")
    assert SLO.resolve_distance({"sl_distance": "2.5"}) == Decimal("2.5")


def test_sl_distance_survives_the_entry_policy_merge():
    """A key missing from ENTRY_POLICY_KEYS is dropped by the merge and the
    setting appears to do nothing — the failure mode #249 called out by name."""
    assert "sl_distance" in ST.ENTRY_POLICY_KEYS

    class _S:
        enabled = True
        account_id, source_id = 5, 7
        entry_policy = {"sl_distance": 3}
    merged = ST.entry_policy([_S()], global_planner={})
    assert merged.get("sl_distance") == 3


# --- the geometry ------------------------------------------------------------

def test_a_buy_stop_lands_the_asked_distance_below_the_entry():
    out, note = SLO.apply(_buy(), Decimal("3"))
    assert note == SLO.APPLIED and out.sl == Decimal("4177")


def test_a_sell_stop_lands_the_asked_distance_above_the_entry():
    out, note = SLO.apply(_sell(), Decimal("3"))
    assert note == SLO.APPLIED and out.sl == Decimal("4183")


def test_a_stop_inside_the_broker_minimum_is_refused_not_placed():
    """An order the broker would reject is not a tighter stop, it is no trade."""
    out, note = SLO.apply(_buy(), Decimal("2"), min_stop_distance=Decimal("5"))
    assert note == SLO.BELOW_BROKER_MIN
    assert out.sl == Decimal("4168")            # untouched


def test_cap_mode_leaves_an_already_tighter_stop_alone():
    tight = _buy(sl="4179")                      # 1 point stop, tighter than 3
    out, note = SLO.apply(tight, Decimal("3"), mode=SLO.MODE_CAP)
    assert note == SLO.ALREADY_TIGHTER and out.sl == Decimal("4179")


def test_fixed_mode_widens_when_that_is_what_was_asked():
    out, note = SLO.apply(_buy(sl="4179"), Decimal("3"), mode=SLO.MODE_FIXED)
    assert note == SLO.APPLIED and out.sl == Decimal("4177")


def test_a_signal_without_geometry_is_reported_not_guessed():
    class _Bare:
        direction, sl, entry_to, entry_from = "BUY", None, None, None
    _, note = SLO.apply(_Bare(), Decimal("3"))
    assert note == SLO.NO_GEOMETRY


# --- the two that cost money -------------------------------------------------

def test_the_shared_signal_is_never_mutated():
    """The executor builds ONE ParsedSignal and fans it across every account
    (main.py:315). In-place mutation would put acct5's stop on acct7 and acct8,
    which is the A/B comparing something nobody configured."""
    sig = _buy()
    before = sig.sl
    out, note = SLO.apply(sig, Decimal("3"))
    assert note == SLO.APPLIED
    assert sig.sl == before == Decimal("4168")   # the original is untouched
    assert out is not sig and out.sl == Decimal("4177")


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_a_zone_signal_keeps_every_leg_placeable(direction):
    """THE anchoring regression. Two thirds of the book is a zone entry (median
    width $5.00) and the planner fans a zone onto BOTH edges. A stop measured
    from the NEAR edge at a distance narrower than the zone lands on the wrong
    side of the far edge, and build_plan drops those legs as "sl on wrong side of
    entry" — the override would do nothing on most signals, silently."""
    sig = (_buy(entry_from="4180", entry_to="4175", sl="4160")
           if direction == "BUY" else
           _sell(entry_from="4175", entry_to="4180", sl="4195"))
    out, note = SLO.apply(sig, Decimal("3"))
    assert note == SLO.APPLIED

    plan = build_plan(out, current_price=Decimal("4178"))
    assert plan.legs, "a zone signal must still produce legs"
    dropped = [l.skip_reason for l in plan.legs if not l.valid]
    assert not dropped, f"legs dropped after the override: {dropped}"
    # every leg's stop is on the protective side of its own entry
    for leg in plan.legs:
        assert (leg.sl < leg.entry) if direction == "BUY" else (leg.sl > leg.entry)


def test_a_tighter_stop_buys_a_larger_lot_at_the_same_cash_risk():
    """The mechanism under test: sizing reads the plan, so the override must land
    before the plan is built. Same risk budget, tighter stop, more size on."""
    risk = RiskConfig(basis="fixed_cash", value=Decimal("100"), allocation="even")

    base = build_plan(_buy(), current_price=Decimal("4180"))
    size_legs(base.legs, equity=Decimal("10000"), risk=risk, instrument=INSTR)

    tight, _ = SLO.apply(_buy(), Decimal("3"))      # 12-point stop -> 3-point stop
    tightened = build_plan(tight, current_price=Decimal("4180"))
    size_legs(tightened.legs, equity=Decimal("10000"), risk=risk, instrument=INSTR)

    # 12-point stop -> 3-point stop is 4x tighter, so ~4x the lot ...
    ratio = tightened.legs[0].lot / base.legs[0].lot
    assert Decimal("3.9") < ratio < Decimal("4.1"), ratio

    # ... for the SAME budget. Both land just under the $50-per-leg share; they
    # differ only by where the 0.01 lot step rounds, never by more than one step
    # of stop distance.
    for leg, stop in ((base.legs[0], Decimal("12")), (tightened.legs[0], Decimal("3"))):
        assert leg.risk_cash <= Decimal("50")
        assert Decimal("50") - leg.risk_cash < INSTR.lot_step * stop
