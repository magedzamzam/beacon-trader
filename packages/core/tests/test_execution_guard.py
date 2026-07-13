"""Risk-limit guard (#7) + planner TP-geometry bound (#13)."""
from decimal import Decimal

from beacon_core.execution.guard import risk_limit_reason, should_auto_execute
from beacon_core.execution.planner import build_plan
from beacon_core.parsing.models import ParsedSignal


# ---- trust guard (#8) ----
def test_trust_guard():
    assert should_auto_execute(enabled_for_trading=True, is_trusted=True, name="Gold VIP")[0] is True
    assert should_auto_execute(enabled_for_trading=True, is_trusted=False, name="Euvean")[0] is False


# ---- risk limits (#7) ----
def test_risk_limits_disabled_is_noop():
    assert risk_limit_reason(planned_risk=99999, day_realized=-99999,
                             open_risk_symbol=0, open_risk_account=0, cfg=None) is None
    assert risk_limit_reason(planned_risk=99999, day_realized=-99999,
                             open_risk_symbol=0, open_risk_account=0,
                             cfg={"enabled": False}) is None


def test_disabled_master_switch_disables_daily_floor():
    # #65: a PRESENT risk_limits row with enabled:false honours the operator —
    # the daily-loss floor does NOT fire, even far past the (still-set) limit.
    cfg = {"enabled": False, "daily_loss_limit": 5000}
    assert risk_limit_reason(planned_risk=10, day_realized=-6000, open_risk_symbol=0,
                             open_risk_account=0, cfg=cfg) is None


def test_killswitch_is_explicit_and_halts_even_when_disabled():
    # The kill-switch is the one explicit "STOP" flag — it must not be silently
    # disarmed by the master switch. trading_halted:true halts regardless of enabled.
    cfg = {"enabled": False, "trading_halted": True, "daily_loss_limit": 5000}
    assert "halted" in risk_limit_reason(
        planned_risk=10, day_realized=0, open_risk_symbol=0, open_risk_account=0, cfg=cfg)


def test_missing_config_uses_failsafe_default():
    # #19 preserved: an UN-configured install (no risk_limits row) still fails
    # safe — the caller passes DEFAULT_RISK_LIMITS, which is enabled with a floor.
    from beacon_core.execution.guard import DEFAULT_RISK_LIMITS
    assert DEFAULT_RISK_LIMITS["enabled"] is True and DEFAULT_RISK_LIMITS["daily_loss_limit"] > 0
    assert "daily loss limit" in risk_limit_reason(
        planned_risk=10, day_realized=-99999, open_risk_symbol=0,
        open_risk_account=0, cfg=dict(DEFAULT_RISK_LIMITS))


def test_daily_loss_limit_zero_disables_floor():
    # enabled but daily_loss_limit:0 -> floor is off (the documented workaround).
    cfg = {"enabled": True, "daily_loss_limit": 0}
    assert risk_limit_reason(planned_risk=10, day_realized=-99999, open_risk_symbol=0,
                             open_risk_account=0, cfg=cfg) is None


def test_per_signal_ceiling():
    cfg = {"enabled": True, "daily_loss_limit": 500, "per_signal_max_pct_of_daily": 0.20}
    # ceiling = 500 * 0.20 = 100; a 5880 plan (the real #7 case) is blocked
    assert "per-signal" in risk_limit_reason(
        planned_risk=5880, day_realized=0, open_risk_symbol=0, open_risk_account=0, cfg=cfg)
    # within the ceiling -> allowed
    assert risk_limit_reason(planned_risk=80, day_realized=0, open_risk_symbol=0,
                             open_risk_account=0, cfg=cfg) is None


def test_daily_circuit_breaker():
    cfg = {"enabled": True, "daily_loss_limit": 500}
    assert "daily loss limit" in risk_limit_reason(
        planned_risk=10, day_realized=-500, open_risk_symbol=0, open_risk_account=0, cfg=cfg)
    assert risk_limit_reason(planned_risk=10, day_realized=-499, open_risk_symbol=0,
                             open_risk_account=0, cfg=cfg) is None


def test_open_risk_caps():
    cfg = {"enabled": True, "max_open_risk_per_symbol": 1000, "max_open_risk_per_account": 2000}
    assert "symbol" in risk_limit_reason(planned_risk=300, day_realized=0,
                                         open_risk_symbol=800, open_risk_account=800, cfg=cfg)
    assert "account" in risk_limit_reason(planned_risk=300, day_realized=0,
                                          open_risk_symbol=0, open_risk_account=1800, cfg=cfg)
    assert risk_limit_reason(planned_risk=100, day_realized=0, open_risk_symbol=100,
                             open_risk_account=100, cfg=cfg) is None


# ---- planner TP geometry bound (#13) ----
def _sig(tps):
    return ParsedSignal(symbol="XAUUSD", direction="SELL", entry_from=Decimal("4179.9"),
                        entry_to=Decimal("4179.9"), sl=Decimal("4185.0"),
                        tps=[Decimal(str(t)) for t in tps])


def test_implausible_tp_is_skipped():
    # tp=1530 vs entry 4179.9 is ~63% away -> skipped; a real tp (4150) survives
    plan = build_plan(_sig([1530, 4150]), current_price=Decimal("4179.9"),
                      max_tp_distance_pct=Decimal("0.5"))
    by_tp = {l.tp: l for l in plan.legs}
    assert by_tp[Decimal("1530")].valid is False
    assert "implausibly far" in by_tp[Decimal("1530")].skip_reason
    assert by_tp[Decimal("4150")].valid is True


def test_no_bound_keeps_all():
    plan = build_plan(_sig([1530]), current_price=Decimal("4179.9"), max_tp_distance_pct=None)
    assert plan.legs[0].valid is True     # no bound -> not skipped by distance


# ---- MARKET / "BUY NOW" entry hint (#25) + bounded chase guard (#67) ----
def _msig(hint, direction="SELL", ef="4160", sl="4170", tps=("4150",), et=None):
    return ParsedSignal(symbol="XAUUSD", direction=direction, entry_from=Decimal(ef),
                        entry_to=Decimal(et if et is not None else ef), sl=Decimal(sl),
                        tps=[Decimal(str(t)) for t in tps], order_type_hint=hint)


def test_market_hint_opens_market_now_when_near():
    # #25 preserved: price 4159 is within the chase tolerance of the 4160 sell
    # entry (gap 1 <= 0.25*10) -> still fills MARKET at the live price.
    plan = build_plan(_msig("MARKET"), current_price=Decimal("4159"), honor_market_hint=True)
    assert plan.legs and all(l.order_type == "MARKET" for l in plan.legs)
    assert all(l.entry == Decimal("4159") for l in plan.legs)


def test_limit_hint_still_rests():
    plan = build_plan(_msig("LIMIT"), current_price=Decimal("4155"), honor_market_hint=True)
    assert all(l.order_type == "LIMIT" for l in plan.legs)


def test_market_hint_can_be_disabled():
    # honor_market_hint=false -> the candle-cross path; price 4155 hasn't reached
    # the 4160 sell entry and no candle did -> rests a LIMIT at 4160.
    plan = build_plan(_msig("MARKET"), current_price=Decimal("4155"), honor_market_hint=False)
    assert all(l.order_type == "LIMIT" and l.entry == Decimal("4160") for l in plan.legs)


# --- #67: the reported chase (BUY entry 3976 filled at ~3998) ---
def test_chase_guard_rests_limit_beyond_tolerance_buy():
    # BUY 3976, SL 3966 (10pt stop, tol=2.5), price 3998 -> 22pt chase -> LIMIT@3976.
    plan = build_plan(_msig("MARKET", direction="BUY", ef="3976", sl="3966", tps=("4010",)),
                      current_price=Decimal("3998"))
    assert plan.legs and all(l.order_type == "LIMIT" and l.entry == Decimal("3976")
                             for l in plan.legs)
    d = plan.entry_decisions[0]
    assert d["decision"] == "limit" and d["gap"] == "22"


def test_chase_guard_rests_limit_beyond_tolerance_sell():
    # SELL mirror: 4020, SL 4030, price 3998 -> 22pt chase down -> LIMIT@4020.
    plan = build_plan(_msig("MARKET", direction="SELL", ef="4020", sl="4030", tps=("3990",)),
                      current_price=Decimal("3998"))
    assert all(l.order_type == "LIMIT" and l.entry == Decimal("4020") for l in plan.legs)


def test_chase_guard_market_when_price_better_than_entry():
    # BUY at a price BELOW its own entry (favourable) -> gap 0 -> MARKET.
    plan = build_plan(_msig("MARKET", direction="BUY", ef="3976", sl="3966", tps=("4010",)),
                      current_price=Decimal("3970"))
    assert all(l.order_type == "MARKET" and l.entry == Decimal("3970") for l in plan.legs)


def test_chase_guard_skip_mode():
    plan = build_plan(_msig("MARKET", direction="BUY", ef="3976", sl="3966", tps=("4010",)),
                      current_price=Decimal("3998"), beyond_tolerance="skip")
    assert plan.legs == [] and plan.entry_decisions[0]["decision"] == "skip"


def test_chase_guard_atr_tolerance():
    # tol_r off, ATR tolerance 0.3*100 = 30 >= 22 gap -> MARKET.
    plan = build_plan(_msig("MARKET", direction="BUY", ef="3976", sl="3966", tps=("4100",)),
                      current_price=Decimal("3998"), chase_tolerance_r=Decimal("0"),
                      chase_tolerance_atr=Decimal("0.3"), atr=Decimal("100"))
    assert all(l.order_type == "MARKET" for l in plan.legs)


def test_chase_guard_boundary_is_inclusive():
    # gap exactly == tolerance -> MARKET (<=). BUY 3976 SL 3966 tol 2.5; price 3978.5.
    plan = build_plan(_msig("MARKET", direction="BUY", ef="3976", sl="3966", tps=("4010",)),
                      current_price=Decimal("3978.5"))
    assert all(l.order_type == "MARKET" for l in plan.legs)
