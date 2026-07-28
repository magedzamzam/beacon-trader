"""Entry-guard parity between the single-shot and confirmation-staged planners.

The A-vs-C experiment only means something if both arms take the SAME signals and
drop the SAME legs — only *when/if* each leg deploys may differ. These pin the
guards that were staged-blind: max_tp_distance_pct (#152), the current candle
(#153), beyond_tolerance="skip" (#155), and the per_tp risk match (#154).
"""
from decimal import Decimal

from beacon_core.execution import staging as S
from beacon_core.execution.planner import build_plan
from beacon_core.execution.staging import DEFAULT_STAGED as D
from beacon_core.parsing.models import ParsedSignal
from beacon_core.risk.sizing import (InstrumentSpec, RiskConfig, plan_total_risk,
                                     size_legs)

# A zone SELL mirroring live sig784: zone 4045-4050, SL 4060, price below the zone.
ZONE = dict(entry_from="4045", entry_to="4050", sl="4060",
            tps=["4035", "4030", "4025"])


def _sig(hint=None, **kw):
    a = dict(ZONE); a.update(kw)
    return ParsedSignal(
        symbol="XAUUSD", direction="SELL",
        entry_from=Decimal(a["entry_from"]), entry_to=Decimal(a["entry_to"]),
        sl=Decimal(a["sl"]), tps=[Decimal(t) for t in a["tps"]],
        order_type_hint=hint)


def _staged(price, **kw):
    near, deep = S.zone_edges("SELL", Decimal(ZONE["entry_from"]), Decimal(ZONE["entry_to"]))
    args = dict(direction="SELL", tps=[Decimal(t) for t in ZONE["tps"]],
                near_edge=near, deep_edge=deep, sl=Decimal(ZONE["sl"]), atr=14,
                current_price=Decimal(str(price)), cfg=D)
    args.update(kw)
    return S.build_staged_legs(**args)


def _toe(legs):
    return next((l for l in legs if l.tranche == S.TOE_IN), None)


# ---- #152: max_tp_distance_pct ----------------------------------------------
# A gold signal near 4045 carrying a parse-artifact TP at 1530 (~62% away).
ARTIFACT = ["4035", "4030", "1530"]


def test_single_shot_drops_the_parse_artifact_tp():
    plan = build_plan(_sig(tps=ARTIFACT), current_price=Decimal("4047"),
                      max_tp_distance_pct=Decimal("0.5"))
    bad = [l for l in plan.legs if l.tp == Decimal("1530")]
    assert bad and all(not l.valid for l in bad)
    assert all("implausibly far" in (l.skip_reason or "") for l in bad)


def test_staged_drops_the_same_tp_with_the_same_reason():
    legs = _staged(4047, tps=[Decimal(t) for t in ARTIFACT],
                   max_tp_distance_pct=Decimal("0.5"))
    bad = [l for l in legs if l.tp == Decimal("1530")]
    assert bad and all(not l.valid for l in bad)
    assert all("implausibly far" in (l.skip_reason or "") for l in bad)


def test_dropped_tp_indices_match_across_entry_styles():
    plan = build_plan(_sig(tps=ARTIFACT), current_price=Decimal("4047"),
                      max_tp_distance_pct=Decimal("0.5"))
    legs = _staged(4047, tps=[Decimal(t) for t in ARTIFACT],
                   max_tp_distance_pct=Decimal("0.5"))
    assert ({l.tp_index for l in plan.legs if not l.valid}
            == {l.tp_index for l in legs if not l.valid} == {3})


def test_no_max_tp_pct_keeps_every_leg():
    legs = _staged(4047, tps=[Decimal(t) for t in ARTIFACT], max_tp_distance_pct=None)
    assert all(l.valid for l in legs)


# ---- #153: the current candle -----------------------------------------------
def test_staged_toe_in_honours_a_candle_touch_price_retraced_from():
    # SELL near edge 4045; the candle printed 4046 (touched the zone) but price is
    # back at 4043. Single-shot opens MARKET off the candle high — staged must too.
    plan = build_plan(_sig(), current_price=Decimal("4043"),
                      candle_high=Decimal("4046"), candle_low=Decimal("4042"))
    assert any(l.order_type == "MARKET" for l in plan.legs)

    legs = _staged(4043, candle_high=Decimal("4046"), candle_low=Decimal("4042"))
    toe = _toe(legs)
    assert toe.order_type == "MARKET" and toe.entry == Decimal("4043")  # live price


def test_candle_short_of_the_edge_still_rests():
    legs = _staged(4043, candle_high=Decimal("4044"), candle_low=Decimal("4042"))
    assert _toe(legs).order_type == "LIMIT"


def test_no_candle_data_falls_back_to_the_live_price():
    assert _toe(_staged(4043)).order_type == "LIMIT"
    assert _toe(_staged(4047)).order_type == "MARKET"       # price alone crossed


def test_candle_touch_does_not_deploy_the_runner_or_reclaim():
    # Only the toe-in reads the candle; the tranches stay price/zone-driven.
    legs = _staged(4043, candle_high=Decimal("4046"), candle_low=Decimal("4042"))
    assert next(l for l in legs if l.tranche == S.RUNNER).order_type == "LIMIT"
    assert next(l for l in legs if l.tranche == S.RECLAIM).order_type == "STOP"


# ---- #155: beyond_tolerance="skip" ------------------------------------------
def test_skip_declines_the_whole_signal_on_both_paths():
    # Price 4025 is 20 below the 4045 sell edge; tolerance = 0.25 x |4045-4060| = 3.75.
    plan = build_plan(_sig(hint="MARKET"), current_price=Decimal("4025"),
                      honor_market_hint=True, beyond_tolerance="skip")
    assert all(d["decision"] == "skip" for d in plan.entry_decisions)
    assert not plan.legs                              # nothing to trade

    legs = _staged(4025, market_hint=True, beyond_tolerance="skip")
    assert legs == []                                 # incl. runner + reclaim


def test_skip_within_tolerance_still_trades():
    legs = _staged(4043.4, market_hint=True, beyond_tolerance="skip")
    assert _toe(legs).order_type == "MARKET"


def test_limit_default_rests_instead_of_skipping():
    legs = _staged(4025, market_hint=True, beyond_tolerance="limit")
    assert _toe(legs).order_type == "LIMIT"


def test_skip_needs_the_hint():
    # No MARKET hint -> the chase guard never applies; behaviour is unchanged.
    legs = _staged(4025, market_hint=False, beyond_tolerance="skip")
    assert _toe(legs).order_type == "LIMIT"


# ---- #154: intended total risk across entry styles --------------------------
# Fine lot steps: the two styles place their legs at DIFFERENT entry levels (near
# edge / deep edge / reclaim trigger), so |entry-SL| differs per leg and rounding
# lots to a coarse step leaves cents of noise on either side. The invariant under
# test is the INTENDED risk, not the rounding residue.
INSTR = InstrumentSpec(value_per_point=Decimal("1"), min_lot=Decimal("0.000001"),
                       lot_step=Decimal("0.000001"))
EQUITY = Decimal("100000")


def _risk(**kw):
    return RiskConfig.from_dict(dict(kw))


def _totals(risk_cfg):
    # Price BELOW the sell zone: neither edge is crossed, so the single-shot
    # planner rests a LIMIT at each edge and the zone genuinely fans out to
    # 2 legs per tp_index. (Above the zone both edges count as crossed and
    # build_plan collapses them into ONE market fill — no double count to see.)
    plan = build_plan(_sig(), current_price=Decimal("4040"))
    assert len(plan.legs) == 2 * len(ZONE["tps"])       # guard the premise
    single = size_legs(plan.legs, equity=EQUITY, risk=risk_cfg, instrument=INSTR)
    staged = size_legs(_staged(4040), equity=EQUITY, risk=risk_cfg, instrument=INSTR)
    return plan_total_risk(single), plan_total_risk(staged)


def _close(x, y, tol=Decimal("0.05")):
    return abs(x - y) <= tol


def test_even_allocation_already_matches():
    a, c = _totals(_risk(basis="capital_percent", value="1.0", allocation="even"))
    assert _close(a, c), (a, c)


def test_per_tp_double_counts_on_a_zone_signal_by_default():
    # The documented status quo: single-shot fans BOTH zone edges onto every TP.
    a, c = _totals(_risk(allocation="per_tp",
                         per_tp_percent={"1": "4.0", "2": "2.0", "3": "1.5"}))
    assert _close(a, c * 2), (a, c)


def test_per_tp_split_across_entries_restores_the_match():
    a, c = _totals(_risk(allocation="per_tp",
                         per_tp_percent={"1": "4.0", "2": "2.0", "3": "1.5"},
                         per_tp_split_across_entries=True))
    assert _close(a, c), (a, c)


def test_split_flag_defaults_off():
    assert RiskConfig.from_dict({}).per_tp_split_across_entries is False


def test_split_flag_is_a_noop_for_even_allocation():
    a, c = _totals(_risk(allocation="even", value="1.0",
                         per_tp_split_across_entries=True))
    assert _close(a, c), (a, c)
