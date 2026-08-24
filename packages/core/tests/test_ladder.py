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


def _one_zone(**kw):
    """A single-level signal — entry_from == entry_to — so it reads the 1-ZONE
    half of the grid. `_sig()` is a zone and reads the other half."""
    base = dict(entry_from="4180", entry_to="4180", sl="4168",
                tps=("4190", "4200", "4210"))
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
    rungs = L.plan_ladder(_sig(entry_to="4176"))
    L.size_ladder(rungs, budget=Decimal("300"), instrument=INSTR)
    rungs = L.plan_ladder(_one_zone())          # the 1-ZONE cell has a MID rung
    L.size_ladder(rungs, budget=Decimal("300"), instrument=INSTR)
    entry_rung = next(r for r in rungs if r.entry == Decimal("4180"))
    mid_rung = next(r for r in rungs if r.entry == Decimal("4174"))
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


@pytest.mark.parametrize("zone", [L.ZONE_SINGLE, L.ZONE_RANGE])
@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 7, 8])
def test_every_cell_cancels_on_tp1_and_never_makes_a_leg_for_it(zone, n):
    rows = L.DEFAULT_MATRIX[zone][n]
    assert L.cancel_rows(rows) == [L.WHEN_TP1]
    sig = _one_zone() if zone == L.ZONE_SINGLE else _sig()
    assert all(r.tranche != L.WHEN_TP1 for r in L.plan_ladder(sig, rows))


def test_the_trigger_level_is_carried_on_every_deferred_rung():
    """A deferred rung carries the level it WAITS FOR — which is its trigger's
    level, not its own entry. Those coincide for `mid / POSITION / MID` and part
    company for `mid / STOP / ENTRY-FROM`, which waits at MID and rests at the
    entry."""
    sig = _sig()                                    # a zone: entry_to and mid triggers
    want = {L.WHEN_ENTRY_TO: Decimal("4176"),
            L.WHEN_MID: L.mid_level(Decimal("4176"), Decimal("4168"))}
    for r in L.plan_ladder(sig):
        if r.tranche == L.WHEN_SIGNAL:
            assert r.trigger is None                # goes out now
        else:
            assert r.trigger == want[r.tranche]


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


@pytest.mark.parametrize("zone", [L.ZONE_SINGLE, L.ZONE_RANGE])
@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 7, 8])
def test_every_cell_of_the_grid_is_valid_by_its_own_rules(zone, n):
    rows = L.DEFAULT_MATRIX[zone][n]
    assert L.clean_ladder(rows) == rows

# --- covering the spectrum #250 specifies ------------------------------------

@pytest.mark.parametrize("n_tp", [2, 3, 4, 5, 6, 7, 8])
def test_every_take_profit_gets_a_rung(n_tp):
    """The default table stopped at TP3, so a 4- or 5-TP signal silently left its
    deepest targets untraded — and this book runs ladders 2-5 deep. Written to
    depth, the drop rule handles the shrinking end and nothing is abandoned at
    the deep end."""
    tps = ["4190", "4200", "4210", "4220", "4230", "4240", "4250", "4260"][:n_tp]
    rungs = L.plan_ladder(_sig(tps=tps))
    assert sorted({r.tp_index for r in rungs}) == list(range(1, n_tp + 1))


def test_a_table_deeper_than_the_signal_still_only_trades_what_exists():
    rungs = L.plan_ladder(_sig(tps=("4190", "4200")))
    assert sorted({r.tp_index for r in rungs}) == [1, 2]


# --- the level a rung waits for is not the level it rests at -----------------

def test_a_rung_waits_for_its_trigger_not_its_own_entry():
    """`mid / open / STOP / ENTRY-FROM / TP3` must wait for MID and then place a
    STOP back at the entry. Reading the trigger off the rung's own entry made it
    wait on ENTRY-FROM — which a BUY is already at or below — so it deployed at
    once instead of at MID."""
    sig = _sig(entry_from="4180", entry_to="4176", sl="4168",
               tps=("4190", "4200", "4210"))
    rows = [L._row(L.WHEN_SIGNAL, L.DO_OPEN, L.ORDER_POSITION, L.LVL_ENTRY_FROM, 1),
            L._row(L.WHEN_MID, L.DO_OPEN, L.ORDER_STOP, L.LVL_ENTRY_FROM, 3)]
    rung = next(r for r in L.plan_ladder(sig, rows) if r.tp_index == 3)
    assert rung.entry == Decimal("4180")          # rests at the entry ...
    assert rung.trigger == Decimal("4172")        # ... but waits for MID
    assert rung.trigger != rung.entry


@pytest.mark.parametrize("when,want", [
    (L.WHEN_ENTRY_FROM, "4180"), (L.WHEN_ENTRY_TO, "4176"),
    (L.WHEN_MID, "4172"), (L.WHEN_TP1, "4190"),
])
def test_every_level_can_also_be_a_trigger(when, want):
    """What makes the two-level ladders expressible at all: they are built around
    the far-edge LIMIT filling, and price reaching ENTRY-TO *is* that fill."""
    sig = _sig(entry_from="4180", entry_to="4176", sl="4168",
               tps=("4190", "4200", "4210"))
    assert L.trigger_level(when, sig) == Decimal(want)


def test_the_signal_trigger_has_no_level():
    assert L.trigger_level(L.WHEN_SIGNAL, _sig()) is None


# --- one table, both entry shapes --------------------------------------------

def test_the_two_level_ladder_reproduces_250s_zone_spec():
    """POSITION at the near edge and a LIMIT at the far one; the LIMIT filling
    arms a STOP back at the near edge; MID arms one at the far edge."""
    sig = _sig(entry_from="4180", entry_to="4176", sl="4168",
               tps=("4190", "4200", "4210"))
    rungs = L.plan_ladder(sig, L.DEFAULT_MATRIX[L.ZONE_RANGE][3])
    shape = {(str(r.trigger), r.order_type, str(r.entry), r.tp_index) for r in rungs}
    # "1 position at ENTRY-FROM -> TP1, 1 position at ENTRY-FROM -> TP2,
    #  a LIMIT at ENTRY-TO -> TP1, a LIMIT at ENTRY-TO -> TP3"
    assert ("None", "MARKET", "4180", 1) in shape   # POSITION -> MARKET
    assert ("None", "MARKET", "4180", 2) in shape
    assert ("None", "LIMIT", "4176", 1) in shape
    assert ("None", "LIMIT", "4176", 3) in shape
    # "LIMITs filled -> open a STOP order at ENTRY-FROM -> TP2"
    assert ("4176", "STOP", "4180", 2) in shape
    # "Price reaches MID -> open a STOP order at ENTRY-TO -> TP3"
    assert ("4172", "STOP", "4176", 3) in shape


def test_a_zone_cell_may_place_two_orders_on_one_level_for_one_target():
    """#250's 3-TP zone ladder opens a position at ENTRY-FROM for TP2 AND later
    arms a STOP there for TP2 as well — the confirmation adding size. Collapsing
    those as duplicates would silently delete half the ladder, which is why the
    grid replaced de-duplication rather than keeping both."""
    sig = _sig(entry_from="4180", entry_to="4176", sl="4168",
               tps=("4190", "4200", "4210"))
    at_entry_for_tp2 = [r for r in L.plan_ladder(sig, L.DEFAULT_MATRIX[L.ZONE_RANGE][3])
                        if r.entry == Decimal("4180") and r.tp_index == 2]
    assert len(at_entry_for_tp2) == 2
    assert {r.order_type for r in at_entry_for_tp2} == {"MARKET", "STOP"}


def test_the_grid_picks_the_cell_that_matches_the_signal():
    """Which ladder runs follows from the SIGNAL — its entry shape and its TP
    count — not from the channel. That is what the grid buys: #250's 3-TP and
    4-TP ladders are different shapes, not one truncated, so no single table
    could have been both."""
    assert L.zone_kind(_one_zone()) == L.ZONE_SINGLE
    assert L.zone_kind(_sig()) == L.ZONE_RANGE
    assert L.rows_for(None, _one_zone(tps=("4190", "4200"))) is L.DEFAULT_MATRIX[L.ZONE_SINGLE][2]
    assert L.rows_for(None, _sig(tps=("4190", "4200", "4210"))) is L.DEFAULT_MATRIX[L.ZONE_RANGE][3]


def test_a_signal_deeper_than_the_grid_reads_the_deepest_cell():
    deep = _one_zone(tps=tuple(str(4190 + 10 * i) for i in range(11)))
    assert len(deep.tps) == 11
    assert L.rows_for(None, deep) is L.DEFAULT_MATRIX[L.ZONE_SINGLE][L.MAX_TPS]


def test_a_stored_grid_overlays_the_defaults_rather_than_replacing_them():
    """Editing one cell must not blank the fifteen nobody touched."""
    mine = [{"when": "signal", "action": "open", "order": "POSITION",
             "level": "ENTRY_FROM", "target": 1}]
    grid = L.matrix_with_defaults({"1": {"3": mine}})
    assert grid[L.ZONE_SINGLE][3] == mine                       # my edit
    assert grid[L.ZONE_SINGLE][2] == L.DEFAULT_MATRIX[L.ZONE_SINGLE][2]   # untouched
    assert grid[L.ZONE_RANGE][3] == L.DEFAULT_MATRIX[L.ZONE_RANGE][3]


def test_an_unreadable_stored_grid_falls_back_to_the_defaults():
    assert L.matrix_with_defaults("nonsense") == L.DEFAULT_MATRIX
    assert L.matrix_with_defaults(None) == L.DEFAULT_MATRIX


@pytest.mark.parametrize("bad", [
    {"9": {"3": []}},                                     # no such zone
    {"1": {"99": [{"when": "signal", "action": "open", "order": "POSITION",
                   "level": "ENTRY_FROM", "target": 1}]}},   # TP count out of range
    {"1": {"3": [{"when": "whenever", "action": "open", "order": "POSITION",
                  "level": "MID", "target": 1}]}},           # bad row
])
def test_a_bad_grid_is_refused_with_the_cell_named(bad):
    with pytest.raises(ValueError):
        L.clean_matrix(bad)


def test_deduping_never_drops_a_genuinely_distinct_rung():
    """Same target at DIFFERENT levels is the ladder working as intended, and must
    survive: TP2 taken at MID and TP2 taken at the entry are different trades."""
    sig = _sig(entry_from="4180", entry_to="4176", sl="4168", tps=("4190", "4200"))
    rows = [L._row(L.WHEN_SIGNAL, L.DO_OPEN, L.ORDER_POSITION, L.LVL_ENTRY_FROM, 1),
            L._row(L.WHEN_MID, L.DO_OPEN, L.ORDER_POSITION, L.LVL_MID, 2),
            L._row(L.WHEN_ENTRY_TO, L.DO_OPEN, L.ORDER_STOP, L.LVL_ENTRY_FROM, 2)]
    rungs = L.plan_ladder(sig, rows)
    assert len(rungs) == 3
    assert {r.entry for r in rungs if r.tp_index == 2} == {Decimal("4172"), Decimal("4180")}


def test_the_deep_default_still_risks_what_the_single_shot_would_have():
    """More rungs must not mean more money — the budget is split further, not
    topped up."""
    risk = RiskConfig(basis="capital_percent", value=Decimal("1"), allocation="even")
    sig = _sig(tps=("4190", "4200", "4210", "4220", "4230"))
    target, total, rungs = _both_totals(sig, risk)
    # A zone cell can carry more rungs than the signal has TPs — several orders
    # can target one TP from different levels. What must not change is the total.
    assert len(rungs) >= 5
    assert total <= target
    assert target - total < Decimal(len(rungs)) * INSTR.lot_step * Decimal("40")


# --- what a rung really risks -------------------------------------------------
def _real_risk(rung, market):
    """What this rung LOSES if it fills and the stop is hit.

    Measured from the price it ACTUALLY fills at, derived here from the market
    rather than read off the rung — reading `rung.entry` would just re-state
    whatever the planner believed, which is the thing under test. A MARKET rung
    fills at its trigger if it has one and at the market if it does not; a
    LIMIT/STOP fills at its own resting level or not at all."""
    if rung.order_type == "MARKET":
        fills_at = rung.trigger if rung.trigger is not None else Decimal(str(market))
    else:
        fills_at = rung.entry
    return rung.lot * abs(fills_at - rung.sl)


def test_a_market_rung_is_budgeted_on_the_price_it_will_fill_at():
    """The risk-parity guarantee, at the level it can actually break.

    `lot = risk_cash / |entry - sl|`, so budgeting a MARKET rung on its NOMINAL
    level while it fills somewhere else puts on a position whose real risk is
    scaled by the ratio of the two distances. A SELL whose near edge is 4649 with
    the stop at 4657 was sized on 8 points and filled at 4645, where the real
    distance is 12: the same rung carrying 1.5x the money it declared."""
    sig = ParsedSignal(symbol="XAUUSD", direction="SELL",
                       entry_from=Decimal("4649"), entry_to=Decimal("4653"),
                       sl=Decimal("4657"),
                       tps=[Decimal(t) for t in ("4650", "4646", "4643", "4637")])
    rows = [L._row(L.WHEN_SIGNAL, L.DO_OPEN, L.ORDER_POSITION, L.LVL_ENTRY_FROM, 3)]
    rungs = L.plan_ladder(sig, rows, current_price=Decimal("4645"))
    L.size_ladder(rungs, budget=Decimal("200"), instrument=INSTR)
    assert rungs and rungs[0].order_type == "MARKET"
    # the fill price, not the near edge, is what it was sized on
    assert rungs[0].entry == Decimal("4645")
    assert abs(_real_risk(rungs[0], Decimal("4645")) - rungs[0].risk_cash) <= Decimal("1")
    # and to name the number this is defending: the near edge is 4 points closer
    # to the stop than the fill is, so the old sizing was 12/8 = 1.5x the budget.
    nominal = Decimal("200") / abs(sig.entry_from - sig.sl)
    assert nominal * abs(Decimal("4645") - sig.sl) > Decimal("290")


def test_a_deferred_market_rung_is_budgeted_on_its_trigger():
    """A rung waiting on MID does not fill at MID's nominal distance from the
    stop by accident — it fills BECAUSE price reached MID, so MID is its entry.
    Sizing it on the level column (ENTRY-FROM, say) while it fills at MID is the
    same error one trigger later."""
    sig = _one_zone(entry_from="4000", entry_to="4000", sl="3980",
                    tps=("4010", "4020", "4030"))
    mid = L.mid_level(sig.entry_to, sig.sl)
    rows = [L._row(L.WHEN_MID, L.DO_OPEN, L.ORDER_POSITION, L.LVL_ENTRY_FROM, 2)]
    rungs = L.plan_ladder(sig, rows, current_price=Decimal("4002"))
    L.size_ladder(rungs, budget=Decimal("100"), instrument=INSTR)
    assert len(rungs) == 1
    assert rungs[0].entry == mid, "it fills where its trigger is, not at ENTRY-FROM"
    assert abs(_real_risk(rungs[0], Decimal("4002")) - rungs[0].risk_cash) <= Decimal("1")


def test_the_whole_default_grid_risks_only_what_it_was_given():
    """Every cell, both zone shapes, one to eight TPs: fill everything, hit the
    stop, and never exceed the budget. The per-rung fix above is only worth
    having if it holds for the tables the platform actually ships with."""
    budget = Decimal("300")
    for direction in ("BUY", "SELL"):
        for n in range(1, L.MAX_TPS + 1):
            for zone in (True, False):
                sig = (_sig(tps=tuple(str(4010 + 10 * i) for i in range(n)))
                       if direction == "BUY" else
                       _sell(tps=tuple(str(4170 - 10 * i) for i in range(n))))
                if not zone:
                    sig.entry_to = sig.entry_from
                # Sweep the market AWAY from the entry as well as onto it. At the
                # edge itself a MARKET rung's fill and its nominal level coincide
                # and the bug hides; it only shows when the two differ, which is
                # the ordinary case — the signal is read seconds after it prints.
                for away in (Decimal("0"), Decimal("3"), Decimal("-3")):
                    px = sig.entry_from + (away if direction == "BUY" else -away)
                    rungs = L.plan_ladder(sig, L.rows_for(None, sig), current_price=px)
                    if not rungs:
                        continue
                    L.size_ladder(rungs, budget=budget, instrument=INSTR)
                    total = sum(_real_risk(r, px) for r in rungs)
                    assert total <= budget + Decimal("1"), (
                        f"{direction} zone={zone} tps={n} px={px}: "
                        f"risked {total} of {budget}")


def test_rungs_sharing_a_trigger_can_want_different_order_types():
    """Why a tranche cannot carry ONE order type for all its legs.

    A tranche groups rungs by the price they WAIT FOR. The shipped 1-zone cells
    from three TPs up put a MARKET rung and a STOP rung on the same `mid`
    trigger — one takes size at MID, the other rests above it and fills only if
    price comes back. Deploying both as whichever type the first leg happened to
    have turned the confirmation order into an unconfirmed market fill."""
    mixed = []
    for zone in (L.ZONE_SINGLE, L.ZONE_RANGE):
        for n in range(1, L.MAX_TPS + 1):
            by_trigger = {}
            for r in L.DEFAULT_MATRIX[zone][n]:
                if r.get("action") != L.DO_OPEN:
                    continue
                by_trigger.setdefault(r["when"], set()).add(
                    L.BROKER_ORDER_TYPE.get(r["order"], r["order"]))
            mixed += [(zone, n, w) for w, k in by_trigger.items() if len(k) > 1]
    assert mixed, "if this ever empties, the per-leg placement below is untested"

    sig = _one_zone(entry_from="4000", entry_to="4000", sl="3980",
                    tps=("4010", "4020", "4030"))
    rungs = L.plan_ladder(sig, L.rows_for(None, sig), current_price=Decimal("4002"))
    at_mid = [r for r in rungs if r.tranche == L.WHEN_MID]
    assert {r.order_type for r in at_mid} == {"MARKET", "STOP"}
    # and they rest at DIFFERENT prices, so one mode could not have served both
    assert len({r.entry for r in at_mid}) == 2


# --- which broker order a rung is --------------------------------------------
@pytest.mark.parametrize("planned,expected", [
    ("MARKET", "MARKET"),
    ("LIMIT", "LIMIT"),
    ("STOP", "STOP"),
    (L.ORDER_POSITION, "MARKET"),      # the operator's word for "take it now"
    ("position", "MARKET"),            # case is the operator's, not the code's
    (" stop ", "STOP"),
])
def test_a_rungs_order_type_maps_to_one_broker_order(planned, expected):
    assert L.order_kind(planned) == expected


def test_an_unknown_order_type_becomes_a_market_order():
    """`plan_ladder` already defaults this way. Failing the other way would rest
    an order that waits for a price nobody set, which never fills and never
    expires — the failure mode that is hardest to notice."""
    assert L.order_kind("nonsense") == "MARKET"
    assert L.order_kind(None) == "MARKET"


def test_the_tranches_mode_is_only_a_fallback():
    """The leg's own type wins. Reading the tranche's label instead is what
    market-filled every confirmation STOP that shared a trigger with a MARKET
    rung; the label is kept only for rows written before the fix, where it is
    the sole record of what was intended."""
    assert L.order_kind("STOP", "MARKET") == "STOP"
    assert L.order_kind(None, "STOP") == "STOP"
    assert L.order_kind("", "LIMIT") == "LIMIT"
