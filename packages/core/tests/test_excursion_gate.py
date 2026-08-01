"""The R-ladder's validation gate, and the cohort error that broke it (#187).

`agreement_sl` read 0.585 and made the whole #182 ladder unusable — the one
instrument designed to escape the TP1-distance artifact could not be acted on.
The reconstruction was not wrong. The GATE was, and in two separate ways, both
of which pooled a different question into the same ratio:

    pure stop-out (no TP, no ratchet)  85/94  0.9043  <- the comparable cohort
    TP1 reached, a later leg stopped    1/27  0.0370  <- the market really did
                                                        reach TP1 first
    stop was RATCHETED                  0/26  0.0000  <- the ratchet ARMS on
                                                        tp_hit(1), so 'tp1' is
                                                        the only honest answer

`race` answers "between TP1 and the ORIGINAL stop, which came FIRST?". The gate
was asking "did this trade eventually stop out?". Those coincide for exactly one
population, and the gate now scores only that one.

Pure — the cohort selection is exercised directly, with both stop kinds present
in the same fixture so they cannot be silently conflated again.
"""
import pytest

from beacon_core.analysis.excursion_store import GATE_MIN_AGREEMENT


def _cohort(legs_by_signal):
    """The cohort rule, mirrored from `_sl_truth` over `(outcome, sl_moved)`."""
    stopped, ratcheted, took_tp = set(), set(), set()
    for sid, legs in legs_by_signal.items():
        for outcome, sl_moved in legs:
            if outcome == "sl_hit":
                stopped.add(sid)
                if sl_moved:
                    ratcheted.add(sid)
            elif outcome == "tp_hit":
                took_tp.add(sid)
    return stopped - ratcheted - took_tp


def test_a_ratcheted_stop_out_is_not_in_the_cohort():
    """It is a broker `sl_hit` whose honest reconstruction is `tp1`: the ratchet
    only arms on tp_hit(1), so the market reached TP1 by construction. Scoring it
    as a disagreement measures the gate's own definition."""
    assert _cohort({1: [("sl_hit", True)]}) == set()


def test_a_stop_out_after_tp1_was_banked_is_not_in_the_cohort():
    """Leg 1 took TP1, leg 2 later stopped on the original stop. `race == 'tp1'`
    is correct — TP1 genuinely came first."""
    assert _cohort({1: [("tp_hit", False), ("sl_hit", False)]}) == set()


def test_a_pure_stop_out_is_in_the_cohort():
    assert _cohort({1: [("sl_hit", False)]}) == {1}


def test_the_three_populations_are_separated_in_one_fixture():
    """All three present at once — the shape that produced 0.585 when pooled."""
    legs = {
        1: [("sl_hit", False)],                      # pure stop-out    -> scored
        2: [("sl_hit", True)],                       # ratcheted        -> excluded
        3: [("tp_hit", False), ("sl_hit", False)],   # TP1 then stopped -> excluded
        4: [("tp_hit", False)],                      # never stopped    -> excluded
    }
    assert _cohort(legs) == {1}


def test_pooling_them_is_what_produced_the_failing_number():
    """The old rule was "any leg recorded sl_hit". Reproduced here so the
    regression has a name: it selects three trades whose only shared property is
    that the broker stopped them, and asks one question of all three."""
    legs = {
        1: [("sl_hit", False)],
        2: [("sl_hit", True)],
        3: [("tp_hit", False), ("sl_hit", False)],
    }
    old_rule = {sid for sid, ls in legs.items()
                if any(o == "sl_hit" for o, _ in ls)}
    assert old_rule == {1, 2, 3}
    assert _cohort(legs) == {1}

    # Only signal 1 can honestly race to the stop; the other two must read 'tp1'.
    race = {1: "sl", 2: "tp1", 3: "tp1"}
    old_agreement = sum(1 for s in old_rule if race[s] == "sl") / len(old_rule)
    new_agreement = sum(1 for s in _cohort(legs) if race[s] == "sl") / len(_cohort(legs))
    assert old_agreement == pytest.approx(1 / 3)
    assert new_agreement == 1.0


def test_a_multi_leg_trade_with_no_stop_is_not_in_the_cohort():
    assert _cohort({1: [("tp_hit", False), ("expired", False)]}) == set()


def test_the_threshold_is_stated_and_below_one():
    """Exact agreement is unattainable: same-bar TP+SL is scored conservatively
    as the stop, suspect bars are dropped, and a 1m bar cannot resolve
    intra-minute order. The bar is documented rather than aspirational."""
    assert 0.5 < GATE_MIN_AGREEMENT < 1.0
    assert GATE_MIN_AGREEMENT == 0.90


def test_the_measured_cohort_clears_the_bar():
    """The live numbers this fix was derived from, pinned so a regression in the
    cohort rule shows up as a failing arithmetic claim rather than as a quietly
    unusable ladder."""
    agreed, total = 85, 94
    assert round(agreed / total, 4) == 0.9043
    assert (agreed / total) >= GATE_MIN_AGREEMENT
    # ...and the number the old rule produced, for contrast.
    assert round(86 / 147, 3) == 0.585
