"""The staged-entry ladder (#250).

The test that matters most is the first one: fill every rung, run price to the
stop, and lose exactly what the single-shot entry would have lost. The ladder is
allowed to change WHEN size arrives and never HOW MUCH, and sizing a rung at the
moment it triggers — rather than up front — is the one way it could quietly cost
more than it says.
"""
from decimal import Decimal

import pytest

from beacon_core.execution import ladder as L
from beacon_core.execution.planner import build_plan
from beacon_core.parsing.models import ParsedSignal
from beacon_core.risk.sizing import (InstrumentSpec, RiskConfig, plan_total_risk,
                                     size_legs)

INSTR = InstrumentSpec(value_per_point=Decimal("1"), min_lot=Decimal("0.01"),
                       lot_step=Decimal("0.01"))
EQUITY = Decimal("10000")


def _sig(direction="BUY", entry_from="4180", entry_to="4176", sl="4168",
         tps=("4190", "4200", "4210")):
    return ParsedSignal(symbol="XAUUSD", direction=direction,
                        entry_from=Decimal(entry_from), entry_to=Decimal(entry_to),
                        sl=Decimal(sl), tps=[Decimal(t) for t in tps])


def _sell(**kw):
    base = dict(direction="SELL", entry_from="4176", entry_to="4180", sl="4192",
                tps=("4170", "4160", "4150"))
    base.update(kw)
    return _sig(**base)


def _both_totals(sig, risk, price="4178", rows=None):
    """(single-shot total risk, ladder total risk) for the same signal."""
    single = build_plan(sig, current_price=Decimal(price))
    size_legs(single.legs, equity=EQUITY, risk=risk, instrument=INSTR)
    target = plan_total_risk(single.legs)

    rungs = L.plan_ladder(sig, rows)
    L.size_ladder(rungs, budget=target, instrument=INSTR)
    return target, plan_total_risk(rungs), rungs


# --- THE GUARANTEE -----------------------------------------------------------

@pytest.mark.parametrize("sig", [_sig(), _sell(), _sig(entry_to="4180"),
                                 _sig(tps=("4190", "4200")),
                                 _sig(tps=("4190", "4200", "4210", "4220"))])
def test_a_fully_filled_ladder_loses_what_the_single_shot_would_have(sig):
    """Every rung filled, price to the stop: the same money, not more."""
    risk = RiskConfig(basis="capital_percent", value=Decimal("1"), allocation="even")
    target, total, rungs = _both_totals(sig, risk)
    assert rungs, "the ladder produced no rungs at all"
    assert total <= target, f"ladder risks MORE than single-shot: {total} > {target}"
    # Short only by where each rung's lot rounds down, never by more.
    slack = Decimal(len(rungs)) * INSTR.lot_step * Decimal("40")
    assert target - total < slack, f"ladder risks far less than single-shot: {total} vs {target}"


def test_the_guarantee_holds_under_per_tp_allocation_too():
    """Live accounts all use `even`, where the property is nearly free. per_tp
    sizes each leg independently, so the ladder must still measure the fanout's
    real total rather than assume a budget."""
    risk = RiskConfig(basis="per_tp", value=Decimal("1"), allocation="per_tp",
                      per_tp_percent={1: Decimal("2"), 2: Decimal("1"), 3: Decimal("0.5")})
    target, total, rungs = _both_totals(_sig(), risk)
    assert target > 0 and rungs
    assert total <= target
    assert target - total < Decimal(len(rungs)) * INSTR.lot_step * Decimal("40")


def test_a_rung_nearer_the_stop_carries_a_bigger_lot_for_the_same_money():
    """Why entering at MID is worth anything: the same cash buys more size when
    the stop is closer. Equal risk per rung, unequal lots."""
    rungs = L.plan_ladder(_sig())
    L.size_ladder(rungs, budget=Decimal("300"), instrument=INSTR)
    by_level = {r.entry: r for r in rungs}
    entry_rung = by_level[Decimal("4180")]
    mid_rung = next(r for r in rungs if r.tranche == L.WHEN_MID and r.entry == Decimal("4172"))
    assert mid_rung.entry > entry_rung.sl
    assert mid_rung.lot > entry_rung.lot          # closer stop -> more size
    assert abs(mid_rung.risk_cash - entry_rung.risk_cash) < Decimal("1")


def test_sizing_happens_before_any_rung_triggers():
    """Every rung comes back already sized, so a signal that walks to MID and
    back cannot stack risk by re-sizing on each visit."""
    rungs = L.plan_ladder(_sig())
    L.size_ladder(rungs, budget=Decimal("300"), instrument=INSTR)
    assert all(r.lot and r.risk_cash for r in rungs if r.valid)


# --- the table ---------------------------------------------------------------

def test_a_target_the_signal_does_not_have_is_simply_not_created():
    """Never an error, never substituted with a nearer TP (#250)."""
    two_tp = _sig(tps=("4190", "4200"))
    rungs = L.plan_ladder(two_tp)                  # DEFAULT_LADDER references TP3
    assert {r.tp_index for r in rungs} == {1, 2}
    assert all(r.tp_index <= 2 for r in rungs)


def test_one_table_serves_a_single_level_signal_because_entry_to_collapses():
    """#250 wrote out separate 1-level and 2-level ladders. On a single-level
    signal ENTRY-TO *is* ENTRY-FROM, so the second falls out of the first."""
    rows = [L._row(L.WHEN_SIGNAL, L.DO_OPEN, L.ORDER_POSITION, L.LVL_ENTRY_FROM, 1),
            L._row(L.WHEN_SIGNAL, L.DO_OPEN, L.ORDER_LIMIT, L.LVL_ENTRY_TO, 2)]
    one_level = _sig(entry_from="4180", entry_to="4180")
    rungs = L.plan_ladder(one_level, rows)
    assert len(rungs) == 2
    assert {r.entry for r in rungs} == {Decimal("4180")}


@pytest.mark.parametrize("direction,entry_to,sl,want", [
    ("BUY", "4176", "4168", "4172"),
    ("SELL", "4180", "4192", "4186"),
])
def test_mid_is_halfway_from_the_far_entry_edge_to_the_stop(direction, entry_to, sl, want):
    assert L.mid_level(Decimal(entry_to), Decimal(sl)) == Decimal(want)


@pytest.mark.parametrize("direction,price,level,hit", [
    ("BUY", "4172", "4172", True),      # fell to MID
    ("BUY", "4173", "4172", False),     # not yet
    ("SELL", "4186", "4186", True),     # rose to MID
    ("SELL", "4185", "4186", False),
])
def test_reached_reads_from_the_side_the_direction_approaches(direction, price, level, hit):
    assert L.reached(direction, Decimal(price), Decimal(level)) is hit


def test_a_rung_whose_target_sits_behind_its_own_entry_is_dropped():
    """A MID rung on a signal whose TP1 is between MID and the entry would be
    born already past its target."""
    sig = _sig(tps=("4174", "4200", "4210"))       # TP1 4174 is below MID-ish
    rows = [L._row(L.WHEN_SIGNAL, L.DO_OPEN, L.ORDER_POSITION, L.LVL_ENTRY_FROM, 1),
            L._row(L.WHEN_MID, L.DO_OPEN, L.ORDER_POSITION, L.LVL_MID, 1)]
    rungs = L.plan_ladder(sig, rows)
    assert all(r.entry < r.tp for r in rungs)
    assert len(rungs) == 1                          # the MID rung at 4172 -> TP 4174 survives
    assert rungs[0].tranche == L.WHEN_SIGNAL or rungs[0].entry == Decimal("4172")


def test_cancel_rows_are_instructions_not_legs():
    rungs = L.plan_ladder(_sig())
    assert all(r.tranche != L.WHEN_TP1 for r in rungs)
    assert L.cancel_rows() == [L.WHEN_TP1]


def test_the_trigger_level_is_carried_on_every_deferred_rung():
    rungs = L.plan_ladder(_sig())
    for r in rungs:
        if r.tranche == L.WHEN_SIGNAL:
            assert r.trigger is None                # goes out now
        else:
            assert r.trigger == r.entry             # waits for its level


# --- validation --------------------------------------------------------------

def test_a_valid_table_round_trips():
    rows = L.clean_ladder([
        {"when": "signal", "action": "open", "order": "position",
         "level": "entry_from", "target": "1"},
        {"when": "tp1", "action": "cancel_all"},
    ])
    assert rows[0] == {"when": "signal", "action": "open", "order": "POSITION",
                       "level": "ENTRY_FROM", "target": 1}
    assert rows[1] == {"when": "tp1", "action": "cancel_all"}


def test_no_table_means_no_table():
    assert L.clean_ladder(None) is None
    assert L.clean_ladder([]) is None


@pytest.mark.parametrize("bad", [
    [{"when": "whenever", "action": "open", "order": "POSITION", "level": "MID", "target": 1}],
    [{"when": "signal", "action": "teleport", "order": "POSITION", "level": "MID", "target": 1}],
    [{"when": "signal", "action": "open", "order": "ICEBERG", "level": "MID", "target": 1}],
    [{"when": "signal", "action": "open", "order": "POSITION", "level": "NOWHERE", "target": 1}],
    [{"when": "signal", "action": "open", "order": "POSITION", "level": "MID", "target": 0}],
    [{"when": "signal", "action": "open", "order": "POSITION", "level": "MID", "target": "x"}],
    "not a list",
])
def test_a_malformed_row_is_refused_not_dropped(bad):
    """A ladder silently missing a rung is a different strategy from the one that
    was saved, so this raises rather than filtering."""
    with pytest.raises(ValueError):
        L.clean_ladder(bad)


def test_a_table_that_never_opens_anything_is_refused():
    with pytest.raises(ValueError):
        L.clean_ladder([{"when": "mid", "action": "open", "order": "POSITION",
                         "level": "MID", "target": 1}])


def test_the_default_table_is_valid_by_its_own_rules():
    assert L.clean_ladder(L.DEFAULT_LADDER) == L.DEFAULT_LADDER
