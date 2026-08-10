"""Provenance on a mined filter rule (#201).

Six live rules were mined from ~900 screened configurations and recorded nothing
about it, so "is this signal or winner's curse?" was unanswerable from the
config. These pin the two halves of the answer: a claim that must be well-formed
to be stored at all, and a gate that refuses to ARM a mined rule whose effect was
never measured out of sample.
"""
import pytest

from beacon_core.analysis import provenance as PV


def _rule(**kw):
    r = {"name": "bt_1h_cci_value_gte100", "enabled": True, "action": "skip",
         "mode": "live",
         "when": {"not": {"type": "indicator", "id": "cci", "field": "value",
                          "op": "gte", "value": 100, "timeframe": "1h"}}}
    r.update(kw)
    return r


def _prov(**kw):
    p = {"status": "recorded", "replay_run_id": 37,
         "variant": "only|1h.cci.value.gte100",
         "n_candidates_screened": 250,
         "effect_in_sample": {"n": 68, "mean_r": 0.0514, "net": 698.41},
         "effect_holdout": {"n": 11, "mean_r": 0.0201, "net": 40.0}}
    p.update(kw)
    return p


# --- the claim has to be well-formed ------------------------------------------
def test_a_well_formed_block_round_trips():
    out = PV.clean_provenance(_prov())
    assert out["replay_run_id"] == 37
    assert out["effect_holdout"] == {"n": 11, "mean_r": 0.0201, "net": 40.0}


def test_a_malformed_claim_is_refused_rather_than_half_kept():
    """The opposite of the evaluator's fail-open reading of a rule, and
    deliberately so: a half-kept claim reads as `recorded` to every later
    reviewer, which is worse than no claim at all."""
    for bad in ({"status": "probably"}, {"effect_holdout": 3},
                {"effect_holdout": {"n": "lots"}}, {"train": ["2026-07-05"]}):
        with pytest.raises(ValueError):
            PV.clean_provenance(_prov(**bad))


def test_unrecorded_carries_nothing_but_the_admission():
    """"Nobody knows" is an answer a reviewer can act on. Claiming half of a
    run alongside it would be pretending to know part."""
    out = PV.clean_provenance({"status": "unrecorded", "replay_run_id": 37,
                               "note": "predates #201"})
    assert out == {"status": "unrecorded", "note": "predates #201"}


def test_provenance_is_optional_everywhere():
    assert PV.clean_provenance(None) is None


# --- what counts as mined ------------------------------------------------------
def test_a_bt_named_rule_is_mined_and_so_is_anything_carrying_provenance():
    assert PV.is_mined(_rule())
    assert PV.is_mined({"name": "hand_written", "provenance": {"status": "unrecorded"}})


def test_an_operator_written_rule_is_not_mined():
    """Naming is a convention and conventions drift, so it is not the only
    test — but a rule with neither tell must not be dragged into the gate."""
    assert not PV.is_mined({"name": "skip_adx_trending_1h", "enabled": True})


# --- the gate ------------------------------------------------------------------
def test_a_mined_rule_with_no_provenance_cannot_be_armed():
    v = PV.promotion_check(_rule(), armed=True)
    assert not v["ok"] and v["code"] == "no_provenance"


def test_in_sample_evidence_alone_is_not_evidence():
    """The screen selected ON the in-sample number, so it cannot also be the
    argument for it."""
    r = _rule(provenance=_prov(effect_holdout=None))
    v = PV.promotion_check(r, armed=True)
    assert not v["ok"] and v["code"] == "no_holdout"


def test_a_holdout_of_four_trades_is_not_a_holdout():
    r = _rule(provenance=_prov(effect_holdout={"n": 4, "mean_r": 0.4}))
    v = PV.promotion_check(r, armed=True)
    assert not v["ok"] and v["code"] == "holdout_too_small"


def test_an_effect_that_flipped_sign_out_of_sample_is_refused():
    """Which is what a holdout is FOR. On the real book this catches three of
    the five live rules (fib, cci, order_block all flipped between run 35 and
    run 37) — the refusal is not hypothetical."""
    r = _rule(provenance=_prov(effect_in_sample={"n": 68, "mean_r": 0.0514},
                               effect_holdout={"n": 11, "mean_r": -0.0363}))
    v = PV.promotion_check(r, armed=True)
    assert not v["ok"] and v["code"] == "sign_flip"


def test_a_measured_rule_that_survived_the_split_is_allowed():
    assert PV.promotion_check(_rule(provenance=_prov()), armed=True)["ok"]


def test_an_explicitly_unrecorded_rule_is_allowed_and_says_so():
    """Grandfathering has to be VISIBLE. The rule still runs; a reviewer reading
    it now learns that its origin is unknown, which is an answer."""
    r = _rule(provenance={"status": "unrecorded"})
    v = PV.promotion_check(r, armed=True)
    assert v["ok"] and v["code"] == "unrecorded"
    assert "unrecorded" in PV.promotion_warnings(r, armed=True)[0]


def test_a_shadow_rule_is_never_gated():
    """A shadow rule is being measured, and measurement is the thing we want to
    stay cheap — gating it would push screening back OUT of the system, where
    nothing can see it at all."""
    r = _rule(mode="shadow")
    assert PV.promotion_check(r, armed=False)["ok"]
    assert PV.promotion_warnings(r, armed=False) == []


def test_a_hand_written_rule_is_never_gated():
    assert PV.promotion_check({"name": "skip_adx_trending_1h"}, armed=True)["ok"]


# --- the warnings (loud, never blocking) ---------------------------------------
def test_selection_intensity_is_reported_when_the_screen_was_deep():
    """250 candidates against 11 held-out trades is ~23 per observation. At that
    depth the best-looking rule is expected to look good by chance."""
    assert PV.selection_intensity(_prov()) == round(250 / 11, 2)
    w = PV.promotion_warnings(_rule(provenance=_prov()), armed=True)
    assert any("250 candidates screened" in x for x in w)


def test_no_intensity_without_both_numbers():
    assert PV.selection_intensity({"n_candidates_screened": 250}) is None
    assert PV.selection_intensity({}) is None


def test_heavy_shrinkage_is_flagged_but_not_refused():
    r = _rule(provenance=_prov(effect_in_sample={"n": 68, "mean_r": 0.40},
                               effect_holdout={"n": 30, "mean_r": 0.05}))
    assert PV.promotion_check(r, armed=True)["ok"]
    assert any("less than half" in x for x in PV.promotion_warnings(r, armed=True))


# --- the one line a reviewer reads ---------------------------------------------
def test_shrinkage_puts_all_three_effects_on_one_line():
    out = PV.shrinkage(_rule(provenance=_prov()), live={"n": 60, "mean_r": -0.2088})
    assert "in-sample +0.0514" in out["line"]
    assert "holdout +0.0201" in out["line"]
    assert "live -0.2088 (n=60)" in out["line"]


def test_shrinkage_says_nothing_it_does_not_know():
    out = PV.shrinkage(_rule(provenance=_prov()))
    assert out["line"].endswith("live —")
    assert PV.shrinkage(_rule(provenance={"status": "unrecorded"})) is None
