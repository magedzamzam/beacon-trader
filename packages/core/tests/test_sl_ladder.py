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


# ---- levels_reached: full-ladder hit detection for the SL ratchet (#148) ------
from beacon_core.strategy.rules import levels_reached

# signal 778 ladder: BUY, TPs [4074, 4079, 4084, 4104]. The staging partition put
# TP1 on the toe-in (closes tp_hit), TP2 & TP3 on reclaim STOPs (expire), TP4 on
# the runner (stays open). The exit rule is tp_hit(2) -> move_sl_to entry.
_LADDER = {1: D("4074"), 2: D("4079"), 3: D("4084"), 4: D("4104")}


def test_levels_reached_counts_expired_reclaim_tp():
    # Price traded through TP2 (4079). TP2's leg was a reclaim STOP that expired —
    # it never closed `tp_hit`, so detecting hits off open legs alone dropped it.
    # Over the full ladder, TP2 (and TP1) must register.
    assert levels_reached("BUY", D("4080"), _LADDER) == {1, 2}


def test_levels_reached_staged_group2_ratchets_runner_to_be():
    # The whole point of #148: the open runner leg (TP4) must ratchet to entry
    # once price reaches TP2, exactly like the single-shot arms did.
    tps_hit = levels_reached("BUY", D("4080"), _LADDER)
    rules = [{"trigger": {"type": "tp_hit", "index": 2},
              "action": {"type": "move_sl_to", "target": "entry"}}]
    runner = PositionCtx(side="BUY", entry=D("4069"), current_sl=D("4052"),
                         current_price=D("4080"), tps=_LADDER)
    assert evaluate(runner, rules, tps_hit) == D("4069")     # SL -> entry (BE)


def test_levels_reached_below_tp2_no_ratchet():
    # Before price reaches TP2 the group-2 rule must not fire (no premature BE).
    tps_hit = levels_reached("BUY", D("4075"), _LADDER)      # past TP1 only
    assert tps_hit == {1}
    rules = [{"trigger": {"type": "tp_hit", "index": 2},
              "action": {"type": "move_sl_to", "target": "entry"}}]
    runner = PositionCtx(side="BUY", entry=D("4069"), current_sl=D("4052"),
                         current_price=D("4075"), tps=_LADDER)
    assert evaluate(runner, rules, tps_hit) is None


def test_effective_tps_hit_union_persists_retraced_tp():
    # `achieved` (TP-hit-closed legs) persists across ticks; union with the live
    # full-ladder set means a TP reached-then-retraced still holds. Here price has
    # slipped back below TP2 but TP2's toe-in closed tp_hit on an earlier tick.
    tps_hit = levels_reached("BUY", D("4075"), _LADDER)      # live: only TP1
    achieved = {2}                                           # persisted tp_hit close
    assert (tps_hit | achieved) == {1, 2}


def test_levels_reached_group1_toe_in_still_registers():
    # Regression: group-1 (TP1 -> BE) staged trades — TP1 is the toe-in — must
    # still ratchet. TP1 registers as soon as price reaches 4074.
    assert levels_reached("BUY", D("4074"), _LADDER) == {1}


def test_levels_reached_sell_side():
    ladder = {1: D("99"), 2: D("98"), 3: D("97")}
    assert levels_reached("SELL", D("97.5"), ladder) == {1, 2}


# ---- next_mfe: max-favorable-excursion latch for sub-poll spikes (#149) --------
from beacon_core.strategy.rules import next_mfe


def test_next_mfe_seeds_from_live_price_when_null():
    # First tick / pre-feature trade (NULL MFE) -> seed with the live price;
    # behaviour is identical to detecting off live price alone.
    assert next_mfe("BUY", None, D("4075")) == D("4075")
    assert next_mfe("SELL", None, D("97.5")) == D("97.5")


def test_next_mfe_is_monotonic_buy():
    assert next_mfe("BUY", D("4080"), D("4082")) == D("4082")   # new high advances
    assert next_mfe("BUY", D("4080"), D("4075")) == D("4080")   # retrace never walks back


def test_next_mfe_is_monotonic_sell():
    assert next_mfe("SELL", D("98"), D("97")) == D("97")        # new low advances
    assert next_mfe("SELL", D("98"), D("99")) == D("98")        # retrace never walks back


def test_mfe_latches_retraced_tp_for_staged_runner():
    # THE #149 scenario: price spikes through TP2 (4079) then retraces below it
    # before the next poll. Detecting off the instantaneous price misses TP2;
    # detecting off the MFE keeps it latched so the runner still ratchets.
    mfe = next_mfe("BUY", None, D("4070"))          # tick 1: below TP1
    mfe = next_mfe("BUY", mfe, D("4081"))           # tick 2: spikes through TP2
    mfe = next_mfe("BUY", mfe, D("4076"))           # tick 3: retraced (past TP1, below TP2)
    assert mfe == D("4081")
    # live price on tick 3 alone would report only TP1; the MFE reports TP1+TP2.
    assert levels_reached("BUY", D("4076"), _LADDER) == {1}
    assert levels_reached("BUY", mfe, _LADDER) == {1, 2}
    rules = [{"trigger": {"type": "tp_hit", "index": 2},
              "action": {"type": "move_sl_to", "target": "entry"}}]
    runner = PositionCtx(side="BUY", entry=D("4069"), current_sl=D("4052"),
                         current_price=D("4076"), tps=_LADDER)
    assert evaluate(runner, rules, levels_reached("BUY", mfe, _LADDER)) == D("4069")


# ---- entry_basis: a 0 fill_price is UNKNOWN, not a fill at zero (#159) --------
from beacon_core.strategy.rules import entry_basis


def test_entry_basis_prefers_a_real_fill():
    assert entry_basis(D("4031.64"), D("4030")) == D("4031.64")


def test_entry_basis_falls_back_when_fill_is_unknown():
    # NULL (never reconciled) and 0 (broker sent no level) mean the same thing.
    assert entry_basis(None, D("4030")) == D("4030")
    assert entry_basis(D("0"), D("4030")) == D("4030")
    assert entry_basis(D("0.000000"), D("4030")) == D("4030")
    assert entry_basis(D("-1"), D("4030")) == D("4030")


def test_entry_basis_accepts_non_decimal_inputs():
    # Both come off SQLAlchemy NUMERIC columns; tolerate str/float like the
    # Decimal(str(...)) calls it replaced.
    assert entry_basis(0.0, "4030") == D("4030")
    assert entry_basis("4031.5", "4030") == D("4031.5")


def test_zero_fill_price_still_ratchets_to_breakeven():
    # THE #159 regression: sig811 acct8's tp3 leg was persisted with
    # fill_price=0, so entry resolved to 0, `move_sl_to: entry` targeted 0 —
    # not an improving stop for a BUY — and the leg rode its original 4018 stop
    # while its sibling accounts ratcheted to ~4031. Off leg.entry it moves.
    ctx = PositionCtx(side="BUY", entry=entry_basis(D("0"), D("4030")),
                      current_sl=D("4018"), current_price=D("4036"),
                      tps={1: D("4035"), 2: D("4040"), 3: D("4050")})
    assert ctx.entry == D("4030")
    assert evaluate(ctx, DEFAULT_SL_RULES, {1}) == D("4030")


def test_zero_fill_price_does_not_break_be_lock_at_r():
    # R = |entry - initial_sl| would be a nonsense 4018 off a 0 entry, so the
    # trigger could never fire; off the planned entry R is the real 12 points.
    rules = [{"trigger": {"type": "be_lock_at_r", "r": 0.6},
              "action": {"type": "move_sl_to", "target": "entry"}}]
    ctx = PositionCtx(side="BUY", entry=entry_basis(D("0"), D("4030")),
                      current_sl=D("4018"), current_price=D("4038"),
                      tps={1: D("4035")}, initial_sl=D("4018"))
    assert evaluate(ctx, rules) == D("4030")
