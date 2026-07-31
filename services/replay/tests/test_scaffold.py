"""Generating the live-config baseline (§5).

The gate is only meaningful if the config it runs is genuinely the live one, so
these assert the mapping from the live tables — and, just as importantly, that
the things that CANNOT be read from the ledger are surfaced rather than guessed.
A silently-wrong equity or fx_factor would make every lot wrong by a constant
ratio, which is precisely the kind of error a validation run would report as a
fill-logic problem.
"""
from __future__ import annotations

import json

from harness import scaffold
from harness.variants import build_variant

ACCOUNTS = [
    {"id": 1, "name": "A", "currency": "AEDd", "enabled": True,
     "risk_config": {"basis": "capital_percent", "value": 1.0}},
    {"id": 2, "name": "B", "currency": "AEDd", "enabled": True,
     "risk_config": {"basis": "capital_percent", "value": 1.0}},
]
STRATEGIES = [
    {"account_id": 1, "source_id": 7, "label": "A/TFXC",
     "exit_policy": {"sl_rules": [{"trigger": {"type": "tp_hit", "index": 2},
                                   "action": {"type": "move_sl_to",
                                              "target": "entry"}}]}},
    {"account_id": None, "source_id": None, "label": "base",
     "entry_policy": {"entry_style": "limit", "ttl_minutes": 60},
     "exit_policy": {"sl_rules": [{"trigger": {"type": "tp_hit", "index": 1},
                                   "action": {"type": "move_sl_to",
                                              "target": "entry"}}]}},
    {"account_id": 2, "source_id": None, "label": "B",
     "entry_policy": {"entry_style": "staged",
                      "staged": {"enabled": True, "min_stop_atr": 0.3}}},
]
LIMITS = {"enabled": True, "daily_loss_limit": 500,
          "max_open_risk_per_symbol": 10000, "max_signal_risk_pct": 2.0}
SMAP = {"value_per_point": "1", "min_lot": "0.01", "lot_step": "0.01",
        "min_stop_distance": None}


def _cfg(**kw):
    base = dict(accounts=ACCOUNTS, sources=[], strategies=STRATEGIES,
                account_source_risk=[], risk_limits=LIMITS, symbol_map=SMAP,
                equity=10000)
    base.update(kw)
    return scaffold.build_run_config(**base)


def test_the_output_is_a_valid_variant_the_harness_can_build():
    """A scaffold that emits something `build_variant` chokes on is worse than
    no scaffold — the failure would land at sweep time."""
    cfg = _cfg()
    v = build_variant(cfg["variants"][0])
    assert v.name == "live"
    assert len(v.accounts) == 2
    assert v.digest()


def test_the_scope_cascade_survives_the_round_trip():
    """The whole reason to read `execution_strategies` rather than retype it:
    (1,7) must still beat (Any,Any) after passing through JSON."""
    v = build_variant(_cfg()["variants"][0])
    assert v.resolve(1, 7).sl_rules[0]["trigger"]["index"] == 2   # the (1,7) row
    assert v.resolve(1, 9).sl_rules[0]["trigger"]["index"] == 1   # the base row
    # …and the (1,7) row inherits the entry policy it does not restate
    assert v.resolve(1, 7).entry_policy["ttl_minutes"] == 60


def test_a_staged_block_is_copied_verbatim():
    """A normalised copy is a different config. Fidelity means byte-for-byte."""
    v = build_variant(_cfg()["variants"][0])
    staged = v.resolve(2, 5).entry_policy["staged"]
    assert staged["min_stop_atr"] == 0.3 and staged["enabled"] is True


def test_strategies_are_emitted_most_specific_first():
    """Cosmetic — `resolve_chain` sorts for itself — but a config a human will
    edit should read in the order it resolves."""
    scopes = [(s.get("account_id"), s.get("source_id"))
              for s in _cfg()["variants"][0]["strategies"]]
    assert scopes[0] == (1, 7)
    assert scopes[-1] == (None, None)


def test_per_account_and_per_pair_risk_both_land_in_the_right_slot():
    asr = [{"account_id": 1, "source_id": 7, "enabled": True,
            "risk_config": {"basis": "fixed_cash", "value": 50}}]
    risk = _cfg(account_source_risk=asr)["variants"][0]["risk"]
    assert risk["by_account"]["1"]["value"] == 1.0
    assert risk["by_account_source"]["1:7"]["value"] == 50


def test_a_disabled_risk_override_is_not_emitted():
    """`resolve_risk_config` ignores a disabled override live; emitting it would
    make the replay size differently from the account it is standing in for."""
    asr = [{"account_id": 1, "source_id": 7, "enabled": False,
            "risk_config": {"basis": "fixed_cash", "value": 50}}]
    risk = _cfg(account_source_risk=asr)["variants"][0]["risk"]
    assert risk["by_account_source"] == {}


def test_equity_accepts_one_number_or_a_per_account_map():
    flat = _cfg(equity=12345)["variants"][0]["accounts"]
    assert {a["equity"] for a in flat} == {12345}
    split = _cfg(equity={"1": 1000, "2": 2000})["variants"][0]["accounts"]
    assert {a["id"]: a["equity"] for a in split} == {1: 1000, 2: 2000}


def test_equity_is_always_named_as_needing_review():
    """It is read from the broker, not the ledger. Sizing is only comparable to
    live if the budget is, so this can never be silently defaulted."""
    review = " ".join(_cfg()["_generated"]["_needs_review"])
    assert "equity" in review


def test_a_non_usd_account_gets_an_fx_warning():
    """`AEDd` is the intended account currency (CLAUDE.md §1), so fx_factor=1 is
    wrong for this install — and being wrong by a constant ratio on every lot is
    exactly the failure a gate would misreport as a fill-logic bug."""
    review = " ".join(_cfg()["_generated"]["_needs_review"])
    assert "fx_factor" in review


def test_a_missing_risk_limits_setting_is_flagged_not_defaulted_quietly():
    review = " ".join(_cfg(risk_limits=None)["_generated"]["_needs_review"])
    assert "risk_limits" in review
    assert "DEFAULT_RISK_LIMITS" in review


def test_a_missing_symbol_map_is_flagged_because_every_lot_depends_on_it():
    review = " ".join(_cfg(symbol_map=None)["_generated"]["_needs_review"])
    assert "value_per_point" in review


def test_the_unmodelled_subsystems_are_listed_in_the_config_itself():
    """They are the honest reasons a validation run can disagree with reality
    for a cause that is not a bug in the fill logic — so they travel WITH the
    config, not in a README someone may not open."""
    joined = " ".join(_cfg()["_generated"]["_not_modelled"])
    for expected in ("session risk multiplier", "trend_filter", "cluster",
                     "AI", "#150", "#161", "#159"):
        assert expected in joined


def test_the_config_is_json_serialisable():
    json.dumps(_cfg(), default=str)


def test_the_summary_names_the_scopes_and_the_review_items():
    s = scaffold.summarise(_cfg())
    assert [a["id"] for a in s["accounts"]] == [1, 2]
    assert any("acct=1 src=7" in row["scope"] for row in s["strategies"])
    assert s["needs_review"] and s["not_modelled"]
    assert "daily_loss_limit" in s["risk_limits_keys"]
