"""Entry-guard parity between the single-shot planner and the staged LADDER.

The A-vs-C experiment only means something if both arms take the SAME signals and
carry the SAME total risk — only *when* size arrives may differ. These pin the
guards that were staged-blind when the old engine ran: max_tp_distance_pct (#152),
beyond_tolerance="skip" (#155), and the risk match (#154), now restated against
`execution/ladder.py`.

The risk one changed shape with #250 and is the strongest of the three. The old
engine matched the control only under `allocation="even"`, and silently staked
half or double under `per_tp`; the ladder is sized against the control plan's
MEASURED total, so it matches under any allocation by construction.
"""
from decimal import Decimal

import pytest

from beacon_core.execution import ladder as L
from beacon_core.execution.planner import build_plan
from beacon_core.parsing.models import ParsedSignal
from beacon_core.risk.sizing import (InstrumentSpec, RiskConfig, plan_total_risk,
                                     size_legs)

# A zone SELL mirroring live sig784: zone 4045-4050, SL 4060, price below the zone.
ZONE = dict(entry_from="4045", entry_to="4050", sl="4060",
            tps=["4035", "4030", "4025"])
INSTR = InstrumentSpec(value_per_point=Decimal("1"), min_lot=Decimal("0.01"),
                       lot_step=Decimal("0.01"))
EQUITY = Decimal("10000")


def _sig(hint=None, **kw):
    a = dict(ZONE)
    a.update(kw)
    return ParsedSignal(
        symbol="XAUUSD", direction="SELL",
        entry_from=Decimal(a["entry_from"]), entry_to=Decimal(a["entry_to"]),
        sl=Decimal(a["sl"]), tps=[Decimal(t) for t in a["tps"]],
        order_type_hint=hint)


# ---- #152: max_tp_distance_pct ----------------------------------------------
# A gold signal near 4045 carrying a parse-artifact TP at 1530 (~62% away).
ARTIFACT = ["4035", "4030", "1530"]


def test_single_shot_drops_the_parse_artifact_tp():
    plan = build_plan(_sig(tps=ARTIFACT), current_price=Decimal("4047"),
                      max_tp_distance_pct=Decimal("0.5"))
    bad = [l for l in plan.legs if l.tp == Decimal("1530")]
    assert bad and all(not l.valid for l in bad)
    assert all("implausibly far" in (l.skip_reason or "") for l in bad)


def test_the_ladder_never_creates_a_rung_for_the_artifact_tp():
    """The single-shot planner marks it invalid; the ladder never builds it. Either
    way no order is placed against 1530 — what must not happen is a laddered
    account trading a TP the control account threw away."""
    rungs = L.plan_ladder(_sig(tps=ARTIFACT), max_tp_distance_pct=Decimal("0.5"))
    assert rungs, "the other rungs must survive"
    assert all(r.tp != Decimal("1530") for r in rungs)


def test_the_surviving_tp_indices_match_across_entry_styles():
    plan = build_plan(_sig(tps=ARTIFACT), current_price=Decimal("4047"),
                      max_tp_distance_pct=Decimal("0.5"))
    single = {l.tp_index for l in plan.legs if l.valid}
    laddered = {r.tp_index for r in L.plan_ladder(
        _sig(tps=ARTIFACT), max_tp_distance_pct=Decimal("0.5"))}
    assert laddered <= single
    assert 3 not in laddered and 3 not in single


def test_no_max_tp_pct_keeps_every_target():
    """The guard is opt-in on both paths: unset means nothing is thrown away."""
    rungs = L.plan_ladder(_sig(tps=ARTIFACT))
    assert any(r.tp == Decimal("1530") for r in rungs)


def test_a_tp_inside_the_brokers_minimum_distance_is_dropped_on_both_paths():
    near_tp = ["4044.9", "4030", "4025"]          # 0.1 from a 4045 entry
    plan = build_plan(_sig(tps=near_tp), current_price=Decimal("4047"),
                      min_stop_distance=Decimal("5"))
    assert any(not l.valid and "min distance" in (l.skip_reason or "")
               for l in plan.legs)
    rungs = L.plan_ladder(_sig(tps=near_tp), min_stop_distance=Decimal("5"))
    assert all(r.tp != Decimal("4044.9") for r in rungs)


# ---- #154: the risk match ----------------------------------------------------
def _totals(sig, risk, price="4047"):
    single = build_plan(sig, current_price=Decimal(price))
    size_legs(single.legs, equity=EQUITY, risk=risk, instrument=INSTR)
    target = plan_total_risk(single.legs)
    rungs = L.plan_ladder(sig)
    L.size_ladder(rungs, budget=target, instrument=INSTR)
    return target, plan_total_risk(rungs), rungs


def test_even_allocation_matches():
    risk = RiskConfig(basis="capital_percent", value=Decimal("1"), allocation="even")
    target, total, rungs = _totals(_sig(), risk)
    assert total <= target
    assert target - total < Decimal(len(rungs)) * INSTR.lot_step * Decimal("40")


def test_per_tp_matches_too_which_the_old_engine_never_did():
    """The old staged planner emitted ONE leg per tp_index while the single-shot
    planner fans a zone onto BOTH edges, so under `per_tp` the control account
    staked about twice the staged one and the arms were not comparable (#154).
    The ladder is sized against the control's measured total, so the mismatch
    cannot arise however the allocation is configured."""
    risk = RiskConfig(basis="per_tp", value=Decimal("1"), allocation="per_tp",
                      per_tp_percent={1: Decimal("2"), 2: Decimal("1"), 3: Decimal("0.5")})
    target, total, rungs = _totals(_sig(), risk)
    assert target > 0 and rungs
    assert total <= target
    assert target - total < Decimal(len(rungs)) * INSTR.lot_step * Decimal("40")


@pytest.mark.parametrize("split", [True, False])
def test_the_split_flag_cannot_unbalance_the_ladder(split):
    """`per_tp_split_across_entries` exists to undo the zone doubling. Whichever
    way it is set, the ladder still matches whatever the control ends up at."""
    risk = RiskConfig(basis="per_tp", value=Decimal("1"), allocation="per_tp",
                      per_tp_percent={1: Decimal("2"), 2: Decimal("1"), 3: Decimal("0.5")},
                      per_tp_split_across_entries=split)
    target, total, rungs = _totals(_sig(), risk)
    assert total <= target
    assert target - total < Decimal(len(rungs)) * INSTR.lot_step * Decimal("40")


# ---- #155: the whole-signal skip --------------------------------------------
# beyond_tolerance="skip" is a decision about the SIGNAL, so it has to be taken
# before the ladder is built at all. The executor and the replay sim both ask
# build_plan first and decline the signal when it returns no legs; these pin the
# control-side behaviour that decision reads.
def test_skip_declines_the_whole_signal():
    plan = build_plan(_sig(hint="MARKET"), current_price=Decimal("4020"),
                      beyond_tolerance="skip", chase_tolerance_r=Decimal("0.1"))
    assert not plan.legs, "a chase beyond tolerance must decline the signal"


def test_skip_within_tolerance_still_trades():
    plan = build_plan(_sig(hint="MARKET"), current_price=Decimal("4049"),
                      beyond_tolerance="skip", chase_tolerance_r=Decimal("0.25"))
    assert plan.legs


def test_limit_default_rests_instead_of_skipping():
    plan = build_plan(_sig(hint="MARKET"), current_price=Decimal("4020"),
                      beyond_tolerance="limit", chase_tolerance_r=Decimal("0.1"))
    assert plan.legs and all(l.order_type == "LIMIT" for l in plan.legs)


def test_skip_needs_the_hint():
    """Without a MARKET hint there is nothing to chase, so nothing to decline."""
    plan = build_plan(_sig(), current_price=Decimal("4020"),
                      beyond_tolerance="skip", chase_tolerance_r=Decimal("0.1"))
    assert plan.legs


# ---- what the ladder defers --------------------------------------------------
def test_only_the_signal_rung_goes_out_at_plan_time():
    rungs = L.plan_ladder(_sig())
    now = [r for r in rungs if r.tranche == L.WHEN_SIGNAL]
    later = [r for r in rungs if r.tranche != L.WHEN_SIGNAL]
    assert now and later
    assert all(r.trigger is None for r in now)
    assert all(r.trigger is not None for r in later)
