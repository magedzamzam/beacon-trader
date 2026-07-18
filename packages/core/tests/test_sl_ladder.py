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


# ---- be_lock_at_r: R-relative favorable-excursion trigger (#109) --------------
from beacon_core.strategy.rules import _triggered

_BE_R = [{"trigger": {"type": "be_lock_at_r", "r": "0.6"},
          "action": {"type": "move_sl_to", "target": "entry"}}]


def _rctx(side, entry, initial_sl, price):
    return PositionCtx(side=side, entry=D(entry), current_sl=D(initial_sl),
                       current_price=D(price), tps={}, initial_sl=D(initial_sl))


def test_be_lock_at_r_buy_fires_at_threshold():
    # entry 100, initial SL 88 -> R = 12; 0.6R = 7.2. Below fires nothing; at/above locks to entry.
    assert evaluate(_rctx("BUY", "100", "88", "107"), _BE_R) is None          # +7 < 7.2R
    assert evaluate(_rctx("BUY", "100", "88", "107.2"), _BE_R) == D("100")    # exactly 0.6R
    assert evaluate(_rctx("BUY", "100", "88", "110"), _BE_R) == D("100")      # well past


def test_be_lock_at_r_sell_uses_r_from_initial_sl():
    # SELL entry 100, initial SL 104.5 -> R = 4.5; 0.6R = 2.7 -> price 97.3 or lower fires.
    assert evaluate(_rctx("SELL", "100", "104.5", "97.4"), _BE_R) is None     # -2.6 < 2.7R
    assert evaluate(_rctx("SELL", "100", "104.5", "97.3"), _BE_R) == D("100")


def test_be_lock_at_r_fail_open_without_initial_sl():
    # No R denominator known -> the trigger must be a no-op, never a wrong lock.
    ctx = PositionCtx(side="BUY", entry=D("100"), current_sl=D("88"),
                      current_price=D("120"), tps={}, initial_sl=None)
    assert not _triggered(_BE_R[0]["trigger"], ctx, set())
    assert evaluate(ctx, _BE_R) is None


def test_be_lock_at_r_only_tightens():
    # Already at a better stop than entry -> _is_improvement blocks the (looser) BE lock.
    ctx = _rctx("BUY", "100", "88", "115")
    ctx.current_sl = D("101")                  # trailing stop already above entry
    assert evaluate(ctx, _BE_R) is None

    # A wide-stop channel (R=12) vs a tight one (R=4) at the SAME +5 move: the
    # R-relative rule adapts — tight one has already made >0.6R, wide one hasn't.
    assert evaluate(_rctx("BUY", "100", "96", "105"), _BE_R) == D("100")      # R=4, +5 = 1.25R
    assert evaluate(_rctx("BUY", "100", "88", "105"), _BE_R) is None          # R=12, +5 = 0.42R
