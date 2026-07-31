"""Order lifecycle: fills, exits, the conservative same-bar rule, and the money.

The `settle` assertions matter more than they look: if a leg stopped at its
ORIGINAL stop does not settle at exactly `-risk_cash`, then R is wrong by a
rounding factor on every single trade, and every variant comparison inherits it.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from harness import bars as B
from harness import fills as F
from conftest import T0, bar


def leg(**kw) -> F.SimLeg:
    d = dict(tp_index=1, order_type="LIMIT", entry=4000.0, tp=4010.0, sl=3990.0,
             initial_sl=3990.0, lot=Decimal("0.1"), risk_cash=Decimal("1"))
    d.update(kw)
    return F.SimLeg(**d)


# --- fills --------------------------------------------------------------------
def test_market_fills_at_the_bar_open_on_the_side_we_pay():
    l = leg(order_type="MARKET")
    assert F.try_fill(l, "BUY", bar(T0, 4001, 4002, 4000, 4001))
    assert l.fill_price == pytest.approx(4001.1)        # the ask
    assert l.status == F.OPEN


def test_market_slippage_is_adverse_for_both_directions():
    b = bar(T0, 4001, 4002, 4000, 4001)
    buy = leg(order_type="MARKET")
    F.try_fill(buy, "BUY", b, slippage_points=0.5)
    assert buy.fill_price == pytest.approx(4001.6)      # pays MORE
    sell = leg(order_type="MARKET")
    F.try_fill(sell, "SELL", b, slippage_points=0.5)
    assert sell.fill_price == pytest.approx(4000.4)     # receives LESS


def test_limit_fills_at_its_level_never_better_and_takes_no_slippage():
    """A bar that gaps through a resting limit would in reality fill at the
    better open price. Crediting that improvement is free money the harness
    cannot verify."""
    l = leg(order_type="LIMIT", entry=4000.0)
    assert F.try_fill(l, "BUY", bar(T0, 3990, 3991, 3980, 3985),
                      slippage_points=5.0)
    assert l.fill_price == 4000.0


def test_limit_does_not_fill_when_only_the_bid_reached_the_level():
    l = leg(order_type="LIMIT", entry=4000.0)
    assert F.try_fill(l, "BUY", bar(T0, 4001, 4002, 4000, 4001)) is False
    assert l.status == F.WORKING


def test_stop_fills_at_its_trigger_plus_slippage():
    l = leg(order_type="STOP", entry=4005.0, trigger=4005.0)
    assert F.try_fill(l, "BUY", bar(T0, 4000, 4006, 3999, 4005),
                      slippage_points=0.3)
    assert l.fill_price == pytest.approx(4005.3)


# --- exits --------------------------------------------------------------------
def test_same_bar_tp_and_sl_is_scored_as_the_stop():
    l = leg(status=F.OPEN, fill_price=4000.0)
    assert F.resolve_exit(l, "BUY", bar(T0, 4000, 4015, 3985, 4000))
    assert l.outcome == F.SL_HIT
    assert l.same_bar_ambiguous is True
    assert l.close_price == 3990.0


def test_tp_only_bar_closes_at_the_target_with_no_slippage():
    l = leg(status=F.OPEN, fill_price=4000.0)
    assert F.resolve_exit(l, "BUY", bar(T0, 4000, 4015, 3999, 4012),
                          slippage_points=1.0)
    assert l.outcome == F.TP_HIT
    assert l.close_price == 4010.0
    assert l.same_bar_ambiguous is False


def test_stop_exit_takes_adverse_slippage():
    l = leg(status=F.OPEN, fill_price=4000.0)
    F.resolve_exit(l, "BUY", bar(T0, 4000, 4001, 3985, 3986), slippage_points=0.4)
    assert l.close_price == pytest.approx(3989.6)       # worse than the stop


def test_a_ratcheted_stop_at_entry_is_labelled_breakeven():
    l = leg(status=F.OPEN, fill_price=4000.0, sl=4000.0, sl_moved=True)
    F.resolve_exit(l, "BUY", bar(T0, 4002, 4003, 3999, 3999))
    assert l.outcome == F.BREAKEVEN


def test_a_moved_stop_that_is_not_at_entry_is_still_sl_hit():
    l = leg(status=F.OPEN, fill_price=4000.0, sl=3995.0, sl_moved=True)
    F.resolve_exit(l, "BUY", bar(T0, 4002, 4003, 3990, 3991))
    assert l.outcome == F.SL_HIT


# --- money --------------------------------------------------------------------
def test_a_leg_stopped_at_its_original_stop_settles_at_exactly_minus_risk():
    l = leg(status=F.OPEN, fill_price=4000.0, lot=Decimal("0.1"),
            risk_cash=Decimal("1"))
    F.resolve_exit(l, "BUY", bar(T0, 4000, 4001, 3985, 3986))
    pl = F.settle(l, "BUY", value_per_point=Decimal("1"))
    assert pl == Decimal("-1.0")                      # == -risk_cash, exactly


def test_sell_pnl_is_the_mirror():
    l = leg(status=F.OPEN, fill_price=4000.0, tp=3990.0, sl=4010.0,
            initial_sl=4010.0, lot=Decimal("0.1"))
    F.resolve_exit(l, "SELL", bar(T0, 4000, 4001, 3985, 3986))
    assert l.outcome == F.TP_HIT
    assert F.settle(l, "SELL", value_per_point=Decimal("1")) == Decimal("1.0")


def test_fx_factor_converts_back_to_account_currency():
    l = leg(status=F.OPEN, fill_price=4000.0, lot=Decimal("0.1"))
    F.resolve_exit(l, "BUY", bar(T0, 4000, 4015, 3999, 4012))
    pl = F.settle(l, "BUY", value_per_point=Decimal("1"), fx_factor=Decimal("2"))
    assert pl == Decimal("0.5")                       # 1.0 instrument / fx 2


# --- terminations -------------------------------------------------------------
def test_expiry_leaves_no_pnl_because_the_trade_did_not_happen():
    l = leg()
    F.expire(l, T0)
    assert l.status == F.EXPIRED and l.outcome == F.EXPIRED_OUT
    assert l.realized_pl is None


def test_cancel_is_distinguishable_from_expiry():
    l = leg()
    F.cancel(l, T0, "stopped_out")
    assert l.status == F.CANCELLED and l.outcome is None


def test_horizon_close_marks_to_market_and_is_not_a_win():
    l = leg(status=F.OPEN, fill_price=4000.0)
    F.close_at_horizon(l, "BUY", bar(T0, 4004, 4005, 4003, 4004))
    assert l.outcome == F.HORIZON
    assert l.close_price == pytest.approx(4003.9)     # the bid we would exit on


def test_deploy_starts_a_fresh_ttl_clock():
    l = leg(status=F.PENDING, tranche="runner")
    assert l.is_pending and l.is_live
    l.deploy(T0, "LIMIT", 3995.0)
    assert l.status == F.WORKING and l.entry == 3995.0 and l.placed_at == T0
