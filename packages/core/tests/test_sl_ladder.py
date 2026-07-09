"""The default tiered SL ratchet (#29): TP1 -> entry, TP2 -> TP1, TP3 -> TP2 ..."""
from decimal import Decimal as D

from beacon_core.strategy.rules import (DEFAULT_SL_RULES, PositionCtx, evaluate)
from beacon_core.execution.guard import DEFAULT_RISK_LIMITS, risk_limit_reason


def _ctx(side="BUY", sl=None):
    return PositionCtx(side=side, entry=D("100"), current_sl=sl,
                       current_price=D("103"),
                       tps={1: D("101"), 2: D("102"), 3: D("103")})


def test_tp1_moves_sl_to_entry():
    assert evaluate(_ctx(sl=D("98")), DEFAULT_SL_RULES, {1}) == D("100")


def test_tp2_moves_sl_to_tp1():
    assert evaluate(_ctx(sl=D("100")), DEFAULT_SL_RULES, {1, 2}) == D("101")


def test_tp3_moves_sl_to_tp2():
    assert evaluate(_ctx(sl=D("101")), DEFAULT_SL_RULES, {1, 2, 3}) == D("102")


def test_ratchet_never_loosens():
    # already at TP1 (101); TP1-only rule would target entry (100) — not an
    # improvement for a BUY, so no move.
    assert evaluate(_ctx(sl=D("101")), DEFAULT_SL_RULES, {1}) is None


def test_sell_side_ratchet():
    ctx = PositionCtx(side="SELL", entry=D("100"), current_sl=D("102"),
                      current_price=D("97"), tps={1: D("99"), 2: D("98")})
    assert evaluate(ctx, DEFAULT_SL_RULES, {1}) == D("100")        # -> entry
    ctx.current_sl = D("100")
    assert evaluate(ctx, DEFAULT_SL_RULES, {1, 2}) == D("99")      # -> TP1


# ---- kill switch (#21) + fail-safe default (#19) ----
def test_kill_switch_blocks():
    cfg = dict(DEFAULT_RISK_LIMITS)
    cfg["trading_halted"] = True
    assert "kill switch" in risk_limit_reason(
        planned_risk=10, day_realized=0, open_risk_symbol=0, open_risk_account=0, cfg=cfg)


def test_default_risk_limits_are_armed():
    assert DEFAULT_RISK_LIMITS["enabled"] is True
    assert DEFAULT_RISK_LIMITS["trading_halted"] is False
    # a 5000 plan is blocked by the default per-signal ceiling (500 * 0.5 = 250)
    assert risk_limit_reason(planned_risk=5000, day_realized=0, open_risk_symbol=0,
                             open_risk_account=0, cfg=DEFAULT_RISK_LIMITS)
