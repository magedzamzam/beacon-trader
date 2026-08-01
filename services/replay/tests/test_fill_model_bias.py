"""The fill model's own contribution to `fill_adverse` (#190).

`fill_adverse` was read as evidence about the candle feed's spread. It cannot be
that on its own: the harness refuses to credit gap-open price improvement by
design (`fills.py` choice 2), and that refusal pushes the number positive with no
feed involved at all. #190's acceptance criterion is that the two are separated
before any of the number is attributed to spread — so both halves are pinned
here, including the direction nobody expects (a STOP that gaps through its
trigger FLATTERS the simulator).
"""
from __future__ import annotations

from decimal import Decimal

from harness import bars as B, fills as F, validate

from conftest import T0, bar


def _leg(order_type="LIMIT", entry=4000.0, trigger=None) -> F.SimLeg:
    return F.SimLeg(tp_index=1, order_type=order_type, entry=entry, tp=4010.0,
                    sl=3990.0, initial_sl=3990.0, lot=Decimal("1"),
                    risk_cash=Decimal("100"), trigger=trigger,
                    placed_at=T0)


# --- the predicate ------------------------------------------------------------
def test_a_buy_limit_whose_bar_opened_below_it_records_the_improvement_forgone():
    """Live would have filled at the better open; the harness fills at the level
    and declines the difference. POSITIVE = the simulation got the worse entry."""
    b = bar(T0, 3998.0, 3999.0, 3996.0, 3998.0, spread=0.0)   # opens 2.0 through
    assert B.gap_at_open("BUY", "LIMIT", 4000.0, b) == 2.0


def test_a_sell_limit_is_mirrored_on_the_bid():
    b = bar(T0, 4002.0, 4004.0, 4001.0, 4002.0, spread=0.0)
    assert B.gap_at_open("SELL", "LIMIT", 4000.0, b) == 2.0


def test_a_limit_reached_inside_the_bar_records_nothing():
    """No gap: live would have filled AT the level too, so there is no
    improvement to forgo and nothing to subtract from `fill_adverse`."""
    b = bar(T0, 4002.0, 4003.0, 3999.0, 4002.0, spread=0.0)
    assert B.gap_at_open("BUY", "LIMIT", 4000.0, b) is None


def test_a_stop_that_gapped_through_its_trigger_flatters_the_simulation():
    """The same rule cuts the other way and the sign says so: live pays the open,
    the harness fills at the trigger, so it is BETTER off — NEGATIVE."""
    b = bar(T0, 4003.0, 4005.0, 4002.0, 4004.0, spread=0.0)
    assert B.gap_at_open("BUY", "STOP", 4000.0, b) == -3.0
    b2 = bar(T0, 3997.0, 3998.0, 3995.0, 3996.0, spread=0.0)
    assert B.gap_at_open("SELL", "STOP", 4000.0, b2) == -3.0


def test_a_market_order_has_nothing_to_forgo():
    """It already fills at the open — the very price the LIMIT rule declines."""
    b = bar(T0, 3998.0, 3999.0, 3996.0, 3998.0, spread=0.0)
    assert B.gap_at_open("BUY", "MARKET", 4000.0, b) is None


def test_the_spread_side_is_the_one_the_order_actually_fills_on():
    """A BUY limit fills on the ASK. Measuring the gap against the mid or the bid
    would credit an improvement the fillable side never offered — worth about a
    spread per fill, which is the size of the effect being decomposed."""
    b = bar(T0, 3999.0, 4000.0, 3998.0, 3999.0, spread=1.0)   # open_ask = 3999.5
    assert B.gap_at_open("BUY", "LIMIT", 4000.0, b) == 0.5


# --- the wiring ---------------------------------------------------------------
def test_try_fill_records_the_gap_on_the_leg_it_filled():
    leg = _leg()
    b = bar(T0, 3998.0, 3999.0, 3996.0, 3998.0, spread=0.0)
    assert F.try_fill(leg, "BUY", b) is True
    assert leg.fill_price == 4000.0            # the rule itself is unchanged
    assert leg.gap_at_open == 2.0


def test_slippage_is_not_folded_into_the_gap():
    """Slippage is an operator-configured penalty, not the fill model's doing.
    Charging the model for it would make the decomposition attribute a
    deliberate cost to a modelling choice."""
    leg = _leg(order_type="STOP", entry=4000.0, trigger=4000.0)
    b = bar(T0, 4003.0, 4005.0, 4002.0, 4004.0, spread=0.0)
    assert F.try_fill(leg, "BUY", b, slippage_points=0.7) is True
    assert leg.fill_price == 4000.7
    assert leg.gap_at_open == -3.0             # measured against the TRIGGER


# --- the decomposition --------------------------------------------------------
def _pair(n, *, sim_fill, live_fill, gap=None, order_type="LIMIT", start=0):
    sim = [{"signal_id": start + i, "account_id": 1, "tp_index": 1,
            "direction": "BUY", "fill_price": sim_fill, "outcome": "tp_hit",
            "order_type": order_type, "gap_at_open": gap} for i in range(n)]
    live = [{"signal_id": start + i, "account_id": 1, "tp_index": 1,
             "direction": "BUY", "fill_price": live_fill, "outcome": "tp_hit"}
            for i in range(n)]
    return sim, live


def test_an_adverse_mean_that_is_entirely_the_harness_rule_leaves_no_residual():
    """The finding #190 exists to prevent: +0.5 adverse looks like a wide feed
    spread and is in fact the harness declining 0.5 of gap-open improvement."""
    sim, live = _pair(10, sim_fill=4000.5, live_fill=4000.0, gap=0.5)
    d = validate.compare(sim, live)["fill_adverse"]
    assert d["mean"] == 0.5
    assert d["decomposition"]["by_design_mean"] == 0.5
    assert d["decomposition"]["residual_mean"] == 0.0


def test_the_residual_is_what_a_spread_argument_may_use():
    sim, live = _pair(10, sim_fill=4000.5, live_fill=4000.0, gap=0.2)
    dec = validate.compare(sim, live)["fill_adverse"]["decomposition"]
    assert dec["by_design_mean"] == 0.2
    assert dec["residual_mean"] == 0.3


def test_by_design_uses_the_same_denominator_as_the_adverse_mean():
    """`residual = mean - by_design` is an identity, not an approximation — which
    it stops being the moment the gapped subset gets its own denominator. Half
    these fills gapped by 1.0; averaged over the gapped subset that is 1.0, and
    the subtraction would then report a NEGATIVE residual that does not exist."""
    a_sim, a_live = _pair(5, sim_fill=4000.5, live_fill=4000.0, gap=1.0)
    b_sim, b_live = _pair(5, sim_fill=4000.5, live_fill=4000.0, gap=None,
                          start=100)
    dec = validate.compare(a_sim + b_sim, a_live + b_live)["fill_adverse"]
    assert dec["decomposition"]["n_fills_scored"] == 10
    assert dec["decomposition"]["n_gapped_fills"] == 5
    assert dec["decomposition"]["by_design_mean"] == 0.5      # 5.0 over TEN fills
    assert dec["decomposition"]["residual_mean"] == 0.0
    assert round(dec["mean"] - dec["decomposition"]["by_design_mean"], 5) == \
        dec["decomposition"]["residual_mean"]


def test_a_stop_gap_shows_up_as_the_harness_flattering_itself():
    """A negative by-design contribution means the residual is WORSE than the
    headline — the direction that would otherwise be silently netted away."""
    sim, live = _pair(10, sim_fill=4000.0, live_fill=4000.0, gap=-0.4,
                      order_type="STOP")
    dec = validate.compare(sim, live)["fill_adverse"]["decomposition"]
    assert dec["by_design_mean"] == -0.4
    assert dec["residual_mean"] == 0.4
    assert dec["by_order_type"]["STOP"]["n_gapped"] == 10


def test_the_two_order_types_are_reported_apart_rather_than_netted():
    """LIMIT and STOP contributions have opposite signs and different fixes.
    Pooling them to a single mean can report ~0 while both are large."""
    lim_sim, lim_live = _pair(5, sim_fill=4000.0, live_fill=4000.0, gap=1.0)
    stp_sim, stp_live = _pair(5, sim_fill=4000.0, live_fill=4000.0, gap=-1.0,
                              order_type="STOP", start=100)
    dec = validate.compare(lim_sim + stp_sim,
                           lim_live + stp_live)["fill_adverse"]["decomposition"]
    assert dec["by_design_mean"] == 0.0                  # nets to nothing pooled
    assert dec["by_order_type"]["LIMIT"]["total"] == 5.0
    assert dec["by_order_type"]["STOP"]["total"] == -5.0


def test_a_leg_with_no_comparable_fill_contributes_to_neither_mean():
    """An unknown live fill (#159) is excluded from `fill_adverse`, so its gap
    must be excluded too — otherwise the two means describe different
    populations and the subtraction stops meaning anything."""
    sim, live = _pair(4, sim_fill=4000.5, live_fill=4000.0, gap=0.5)
    ghost_s, ghost_l = _pair(4, sim_fill=4000.5, live_fill=0.0, gap=99.0,
                             start=100)
    dec = validate.compare(sim + ghost_s, live + ghost_l)["fill_adverse"]
    assert dec["n"] == 4
    assert dec["decomposition"]["n_fills_scored"] == 4
    assert dec["decomposition"]["by_design_mean"] == 0.5


def test_no_comparable_fills_reports_no_decomposition_rather_than_zero():
    dec = validate.compare([], [])["fill_adverse"]["decomposition"]
    assert dec["n_fills_scored"] == 0
    assert dec["by_design_mean"] is None and dec["residual_mean"] is None


def test_the_decomposition_does_not_claim_to_settle_the_live_vs_demo_question():
    """A zero residual says the harness explains its own fill error. It says
    nothing about whether the store's LIVE-endpoint bars match the DEMO account
    they back — that needs an overlapping re-pull, and the block must not be
    read as having answered it (#190 defect B)."""
    sim, live = _pair(3, sim_fill=4000.5, live_fill=4000.0, gap=0.5)
    label = validate.compare(sim, live)["fill_adverse"]["decomposition"]["label"]
    assert "spread" in label
    assert "residual_mean" in label
