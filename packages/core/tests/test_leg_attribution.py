"""A leg may only carry money that was settled against its own position (#234).

Every case here is taken from the live book. The defect was not exotic: one
broker close attributed to legs in two different trades, identical to the cent,
lots differing 3.6x, 506 times.
"""
import ast
import io
import os

from beacon_core.db import base as B
from beacon_core.db.models import Leg
from beacon_core.execution import attribution as ATTR


def _txn(deal_id, pl):
    return {"deal_id": deal_id, "pl": pl, "instrument": "GOLD"}


# --- which basis a close is on ----------------------------------------------

def test_the_legs_own_transaction_is_exact():
    assert ATTR.classify(_txn("d1", -100.0), "d1") == ATTR.ATTR_EXACT


def test_somebody_elses_transaction_is_not_this_legs_money():
    """The live case: legs 6288 (trade 1182, lot 13.59) and 6289 (trade 1183,
    lot 3.75) both took -98.99 from one close, eleven seconds apart."""
    assert ATTR.classify(_txn("d_other", -98.99), "d_mine") == ATTR.ATTR_UNATTRIBUTED


def test_a_leg_with_no_position_ref_can_never_match_exactly():
    """Which is the whole population of the defect — the instrument fallback is
    reached only when the ref is missing, so an adopted dealId was ALWAYS a
    guess."""
    assert ATTR.classify(_txn("d1", -100.0), None) == ATTR.ATTR_UNATTRIBUTED
    assert ATTR.classify(_txn("d1", -100.0), "") == ATTR.ATTR_UNATTRIBUTED


def test_no_transaction_at_all_is_the_heuristic_basis():
    assert ATTR.classify(None, "d1") == ATTR.ATTR_HEURISTIC


def test_the_two_sides_may_disagree_on_type():
    """One arrives as broker JSON, the other out of a NUMERIC column."""
    assert ATTR.classify({"deal_id": 4242, "pl": 1.0}, "4242") == ATTR.ATTR_EXACT


# --- what gets written -------------------------------------------------------

def test_an_exact_match_keeps_the_brokers_number():
    assert ATTR.realized_pl_for(ATTR.ATTR_EXACT, broker_pl=-829.42,
                                heuristic_pl=-11.0) == -829.42


def test_an_unattributed_close_stores_NO_money():
    """Not the foreign amount (that is the 47,524.5), and not the price
    estimate either — substituting one would dress an unknown as a measurement
    and read as settled money in every rollup downstream."""
    assert ATTR.realized_pl_for(ATTR.ATTR_UNATTRIBUTED, broker_pl=-829.42,
                                heuristic_pl=-11.0) is None


def test_a_heuristic_close_stores_the_estimate_and_says_so():
    assert ATTR.realized_pl_for(ATTR.ATTR_HEURISTIC, broker_pl=None,
                                heuristic_pl=-11.0) == -11.0


def test_only_an_exact_match_may_brand_the_leg_with_a_deal_id():
    """Adoption is what made it permanent: the leg then carried another
    position's identity, so every later query saw a well-referenced leg."""
    assert ATTR.may_adopt_position_ref(ATTR.ATTR_EXACT) is True
    assert ATTR.may_adopt_position_ref(ATTR.ATTR_UNATTRIBUTED) is False
    assert ATTR.may_adopt_position_ref(ATTR.ATTR_HEURISTIC) is False


# --- what analysis may sum ---------------------------------------------------

def test_only_exact_money_enters_a_statistic():
    assert ATTR.is_auditable(ATTR.ATTR_EXACT)
    for bad in (ATTR.ATTR_UNATTRIBUTED, ATTR.ATTR_HEURISTIC, ATTR.ATTR_DUPLICATE):
        assert not ATTR.is_auditable(bad), bad


def test_unclassified_history_still_counts():
    """NULL is what every row carries until the backfill runs. Excluding it
    would empty every existing report the moment this deploys."""
    assert ATTR.is_auditable(None)


def test_a_report_can_say_what_it_dropped():
    class _L:
        def __init__(self, b):
            self.pl_attribution = b
    split = ATTR.split_by_basis([_L("exact"), _L("exact"), _L("duplicate"), _L(None)])
    assert len(split["exact"]) == 3          # NULL folds in with exact, as above
    assert len(split["duplicate"]) == 1


# --- the structural guarantee ------------------------------------------------

def _close_leg_ast():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.normpath(os.path.join(
        here, "..", "..", "..", "services", "monitor", "main.py"))
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "_close_leg":
            return node
    raise AssertionError("_close_leg not found — did it move?")


def test_close_leg_never_writes_a_position_ref():
    """The one line that made the defect unrecoverable:

        if m is not None and m.get("deal_id") and not leg.broker_position_ref:
            leg.broker_position_ref = str(m.get("deal_id"))

    `m` here could only ever be an instrument-level match — an exact match
    requires the ref to have existed already — so this assigned a FOREIGN deal
    id 521 times across different trades. Asserted over the AST of the real
    module rather than a grep, so prose about it cannot satisfy the test."""
    targets = []
    for node in ast.walk(_close_leg_ast()):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute):
                    targets.append(t.attr)
    assert "broker_position_ref" not in targets, targets
    assert "realized_pl" in targets      # non-vacuous: it does still write money


def test_close_leg_stamps_the_basis_it_used():
    targets = [t.attr for node in ast.walk(_close_leg_ast())
               if isinstance(node, ast.Assign)
               for t in node.targets if isinstance(t, ast.Attribute)]
    assert "pl_attribution" in targets


# --- schema ------------------------------------------------------------------

def test_the_column_exists_in_the_model_and_has_an_alter():
    assert "pl_attribution" in Leg.__table__.columns
    assert any("legs" in s and "pl_attribution" in s for s in B.ADDITIVE_MIGRATIONS)


def test_the_backfill_classifies_rather_than_deletes():
    """The 506 contaminated rows keep their value. Nulling them would restate
    three months of reported P&L by 47,524.5 AED as a side effect of a deploy —
    the operator's call to make deliberately, not a migration's to make
    quietly."""
    bf = [s for s in B.STARTUP_BACKFILLS if "pl_attribution" in s]
    assert len(bf) == 1
    stmt = bf[0]
    assert "SET pl_attribution" in stmt
    assert "realized_pl = NULL" not in stmt and "realized_pl=NULL" not in stmt
    assert "pl_attribution IS NULL" in stmt        # self-limiting
