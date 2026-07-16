"""Risk sizing — per-signal risk cap (#78) and the plan_total_risk aggregation
it relies on. Pure (no DB/broker)."""
from decimal import Decimal

from beacon_core.execution.planner import PlannedLeg
from beacon_core.risk.sizing import (InstrumentSpec, RiskConfig, size_legs,
                                     plan_total_risk, cap_total_risk, resolve_risk_config)


def test_resolve_risk_config_fallback():
    acct = {"basis": "capital_percent", "value": "1.0", "allocation": "even"}
    ovr = {"basis": "capital_percent", "value": "0.5", "allocation": "even"}
    # enabled non-empty override wins
    assert resolve_risk_config(ovr, True, acct) == ovr
    # disabled or empty override -> account risk
    assert resolve_risk_config(ovr, False, acct) == acct
    assert resolve_risk_config({}, True, acct) == acct
    assert resolve_risk_config(None, True, acct) == acct
    # nothing -> {} (RiskConfig.from_dict then applies conservative defaults)
    assert resolve_risk_config(None, True, None) == {}

INSTR = InstrumentSpec(value_per_point=Decimal("1"), min_lot=Decimal("0.01"),
                       lot_step=Decimal("0.01"))


def _leg(entry, sl, tp_index, lot=None):
    return PlannedLeg(side="SELL", entry=Decimal(str(entry)), tp=Decimal("3900"),
                      sl=Decimal(str(sl)), tp_index=tp_index, order_type="LIMIT",
                      lot=(Decimal(str(lot)) if lot is not None else None))


def test_per_tp_stacks_and_plan_total_risk_sums_all_legs():
    # 2 entries x 5 TPs = 10 legs, all sharing the SL -> per_tp risks each leg
    # independently. plan_total_risk already sums EVERY valid leg (no under-count).
    per_tp = {1: Decimal("3"), 2: Decimal("1.5"), 3: Decimal("1"),
              4: Decimal("0.5"), 5: Decimal("0.5")}
    legs = [_leg(4050 if e == 0 else 4060, 4038, idx)
            for e in (0, 1) for idx in range(1, 6)]
    risk = RiskConfig(basis="capital_percent", value=Decimal("1"),
                      allocation="per_tp", per_tp_percent=per_tp)
    size_legs(legs, equity=Decimal("100000"), risk=risk, instrument=INSTR)
    total = plan_total_risk(legs)
    # 2 x (3+1.5+1+0.5+0.5)% of 100k = 2 x 6500 = 13,000 -> the stacking the issue
    # flags. (Slightly under 13k only from per-leg lot rounding, never ~6.5k — so
    # plan_total_risk is NOT under-counting the multi-entry split.)
    assert Decimal("12900") <= total <= Decimal("13000")


def test_cap_scales_down_to_cap():
    per_tp = {1: Decimal("3"), 2: Decimal("1.5"), 3: Decimal("1"),
              4: Decimal("0.5"), 5: Decimal("0.5")}
    legs = [_leg(4050 if e == 0 else 4060, 4038, idx)
            for e in (0, 1) for idx in range(1, 6)]
    risk = RiskConfig(basis="capital_percent", value=Decimal("1"),
                      allocation="per_tp", per_tp_percent=per_tp)
    size_legs(legs, equity=Decimal("100000"), risk=risk, instrument=INSTR)
    cap = Decimal("2000")                      # 2% of 100k
    after = cap_total_risk(legs, cap=cap, instrument=INSTR)
    assert after <= cap                        # bounded
    assert after > cap * Decimal("0.7")        # scaled close to the cap, not zero


def test_cap_noop_when_under_cap():
    legs = [_leg(4050, 4038, 1)]
    risk = RiskConfig(basis="capital_percent", value=Decimal("1"), allocation="even")
    size_legs(legs, equity=Decimal("100000"), risk=risk, instrument=INSTR)
    before = plan_total_risk(legs)
    assert before <= Decimal("2000")
    after = cap_total_risk(legs, cap=Decimal("2000"), instrument=INSTR)
    assert after == before                     # already under cap -> untouched


def test_cap_disabled_when_zero():
    legs = [_leg(4050, 4038, 1, lot="5")]
    legs[0].risk_cash = Decimal("99999")
    assert cap_total_risk(legs, cap=Decimal("0"), instrument=INSTR) == Decimal("99999")


def test_cap_drops_leg_below_min_lot():
    # A tiny cap forces lots below min_lot -> those legs are invalidated, not
    # silently over-risked.
    legs = [_leg(4050, 4038, 1), _leg(4060, 4038, 2)]
    risk = RiskConfig(basis="capital_percent", value=Decimal("5"), allocation="even")
    size_legs(legs, equity=Decimal("100000"), risk=risk, instrument=INSTR)
    after = cap_total_risk(legs, cap=Decimal("0.1"), instrument=INSTR)  # absurdly tight
    assert after <= Decimal("0.1")
    assert any(not l.valid for l in legs)      # at least one dropped, not over-risked


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok ", n)
    print("ALL PASS")
