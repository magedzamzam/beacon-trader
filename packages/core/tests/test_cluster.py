"""Correlation-cluster risk budgeting (#106) — pure allocation math. Runs on a
bare box (no DB). The executor wiring (query + tag) is exercised by the sizing/
guard tests; here we pin the arithmetic the money path depends on."""
from decimal import Decimal

from beacon_core.risk import cluster as CL


def _members(*risks):
    return [CL.ClusterMember(planned_risk=Decimal(str(r))) for r in risks]


# ---- config -----------------------------------------------------------------
def test_merge_config_absent_is_feature_off():
    assert CL.merge_config(None) is None
    assert CL.merge_config({}) is None


def test_merge_config_overlays_defaults_and_validates_mode():
    cfg = CL.merge_config({"enabled": True, "window_minutes": 45,
                           "allocation": "nonsense"})
    assert cfg["enabled"] is True
    assert cfg["window_minutes"] == 45
    assert cfg["allocation"] == "equal"          # invalid mode -> safe default
    assert cfg["mixed_policy"] == "off"          # default preserved


def test_resolve_budget_explicit_then_fallback():
    assert CL.resolve_budget({"budget": 4000}, 2500) == Decimal("4000")
    assert CL.resolve_budget({"budget": None}, 2500) == Decimal("2500")
    assert CL.resolve_budget({"budget": 0}, 2500) == Decimal("2500")


# ---- equal (de-size-to-fit) --------------------------------------------------
def test_equal_first_member_takes_up_to_budget():
    a = CL.allocate(6199, _members(), budget=3000, mode="equal")
    assert a["target_risk"] == "3000"
    assert Decimal(a["scale"]) < 1
    assert a["cluster_size"] == 1
    assert a["limited"] is True


def test_equal_under_budget_passes_through():
    a = CL.allocate(1000, _members(), budget=3000, mode="equal")
    assert a["target_risk"] == "1000"
    assert a["scale"] == "1"
    assert a["limited"] is False


def test_equal_exhausted_budget_rejects_arrival():
    a = CL.allocate(2500, _members(3000), budget=3000, mode="equal")
    assert a["target_risk"] == "0"
    assert Decimal(a["scale"]) == 0                 # caller drops legs below min_lot


def test_six_sell_cluster_aggregate_bounded_to_one_unit():
    # The exact operator failure: 6 SELL, one view, aggregate planned risk 20,174.
    # Under equal de-size-to-fit the cluster aggregate must not exceed one unit.
    budget = Decimal("3000")
    arrivals = [6199, 4292, 1155, 3611, 2500, 2418]
    applied, agg = [], Decimal(0)
    for r in arrivals:
        a = CL.allocate(r, _members(*applied), budget=budget, mode="equal")
        t = Decimal(a["target_risk"])
        applied.append(t)
        agg += t
    assert agg <= budget                            # ~1 unit, not 20,174
    assert agg == Decimal("3000")                   # first fills the budget, rest 0


# ---- decaying ----------------------------------------------------------------
def test_decaying_halves_each_confirmation():
    budget = Decimal("4000")
    # k=0 -> budget*1 (but capped by remaining), k=1 -> budget*0.5, k=2 -> budget*0.25
    a1 = CL.allocate(9999, _members(), budget=budget, mode="decaying", decay=0.5)
    assert Decimal(a1["target_risk"]) == 4000       # min(new, remaining=4000, 4000)
    a2 = CL.allocate(9999, _members(1000), budget=budget, mode="decaying", decay=0.5)
    # remaining = 3000, decay cap = 2000 -> the decay cap binds
    assert Decimal(a2["target_risk"]) == 2000
    a3 = CL.allocate(9999, _members(1000, 2000), budget=budget, mode="decaying", decay=0.5)
    # remaining = 1000, decay cap = 1000 -> 1000
    assert Decimal(a3["target_risk"]) == 1000


def test_decaying_never_exceeds_remaining_budget():
    a = CL.allocate(9999, _members(3900), budget=Decimal("4000"),
                    mode="decaying", decay=0.5)
    assert Decimal(a["target_risk"]) <= Decimal("100")   # only 100 remains


# ---- confidence_weighted -----------------------------------------------------
def test_confidence_weighted_scales_by_weight():
    a = CL.allocate(3000, _members(), budget=5000, mode="confidence_weighted",
                    new_weight="0.5")
    assert a["target_risk"] == "1500.0"             # min(3000, 5000) * 0.5
    # weight 1 (default/unknown) behaves like equal-fit
    b = CL.allocate(3000, _members(), budget=5000, mode="confidence_weighted",
                    new_weight="1")
    assert b["target_risk"] == "3000"


# ---- no budget => pass-through ----------------------------------------------
def test_zero_budget_passthrough():
    a = CL.allocate(3000, _members(1000), budget=0, mode="equal")
    assert a["scale"] == "1"
    assert a["target_risk"] == "3000"
    assert a["limited"] is False


# ---- mixed clusters ----------------------------------------------------------
def test_mixed_none_when_single_direction():
    assert CL.mixed_exposure("SELL", [1000, 2000], []) is None


def test_mixed_reports_net_and_gross():
    m = CL.mixed_exposure("BUY", [660, 1213, 1211], [1957, 1840, 1674])
    assert m["same_dir_count"] == 3 and m["opp_dir_count"] == 3
    assert m["gross_exposure"] == str(Decimal("660") + 1213 + 1211 + 1957 + 1840 + 1674)
    # SELL side (5471) > BUY side (3084) -> net side is SELL, net = 2387
    assert m["net_side"] == "SELL"
    assert m["net_exposure"] == "2387"


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok ", n)
    print("ALL PASS")
