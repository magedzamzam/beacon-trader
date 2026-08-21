"""Staged entry, after #250 removed the engine.

The partition, the break-then-reclaim geometry and the DECIDE pipeline are gone
with the thirteen numbers that tuned them — `test_ladder.py` covers what replaced
them. What is left here is what staged entry still owns: the entry_style enum,
validation of the staged block, and the #158 order-age bounds, whose PRECEDENCE
is the subtle part — a rung deploys late and resets its leg clock, so only the
absolute ceiling can bound the entry.
"""
import pytest

from beacon_core.execution import staging as S
from beacon_core.execution.staging import DEFAULT_STAGED as D


# ---- entry-TTL window: deployed TTL + absolute ceiling (#158) ----
def test_deployed_ttl_defaults_to_the_entry_ttl():
    # 0/unset = inherit, i.e. the behaviour that shipped with #129.
    assert S.deployed_ttl_minutes(D, 60) == 60
    assert S.deployed_ttl_minutes({"deployed_ttl_minutes": 0}, 45) == 45
    assert S.deployed_ttl_minutes({}, 90) == 90


def test_deployed_ttl_overrides_when_set():
    assert S.deployed_ttl_minutes({"deployed_ttl_minutes": 15}, 60) == 15
    assert S.deployed_ttl_minutes({"deployed_ttl_minutes": "20"}, 60) == 20
    assert S.deployed_ttl_minutes({"deployed_ttl_minutes": "junk"}, 60) == 60   # fail-safe


def test_entry_age_ceiling_is_off_by_default():
    assert S.entry_age_exceeded(D, 10_000) is False
    assert S.entry_age_exceeded({"max_entry_age_minutes": 0}, 10_000) is False


def test_entry_age_ceiling_fires_past_the_limit():
    cfg = {"max_entry_age_minutes": 90}
    assert S.entry_age_exceeded(cfg, 89) is False
    assert S.entry_age_exceeded(cfg, 90) is False        # strictly past it
    assert S.entry_age_exceeded(cfg, 91) is True


def _expiry(cfg, leg_age, entry_age, deployed, entry_ttl=60):
    return S.entry_expiry_reason(cfg, leg_age_minutes=leg_age,
                                 entry_age_minutes=entry_age,
                                 entry_ttl_minutes=entry_ttl, deployed=deployed)


def test_expiry_default_config_reproduces_todays_behaviour():
    # A runner deployed at T+45 rests a fresh 60 -> alive at T+104, gone past its own TTL.
    assert _expiry(D, leg_age=59, entry_age=104, deployed=True) is None
    assert _expiry(D, leg_age=61, entry_age=106, deployed=True) == "leg_ttl"
    # A toe-in placed at signal time answers to the entry TTL, not the deployed one.
    assert _expiry(D, leg_age=61, entry_age=61, deployed=False) == "leg_ttl"


def test_expiry_deployed_ttl_shortens_the_resting_window():
    cfg = dict(D, deployed_ttl_minutes=15)
    assert _expiry(cfg, leg_age=16, entry_age=61, deployed=True) == "leg_ttl"
    # ...and leaves a signal-time leg alone: it is not a deployed one.
    assert _expiry(cfg, leg_age=16, entry_age=16, deployed=False) is None


def test_expiry_ceiling_wins_over_a_leg_ttl_that_has_not_elapsed():
    # The whole point: a late deploy reset the leg clock, so only the absolute
    # ceiling can bound the entry.
    cfg = dict(D, max_entry_age_minutes=90)
    assert _expiry(cfg, leg_age=5, entry_age=120, deployed=True) == "max_entry_age"
    assert _expiry(cfg, leg_age=5, entry_age=60, deployed=True) is None


def test_expiry_ceiling_applies_to_the_toe_in_too():
    cfg = dict(D, max_entry_age_minutes=30)
    assert _expiry(cfg, leg_age=10, entry_age=31, deployed=False) == "max_entry_age"


def test_expiry_reports_the_ceiling_when_both_would_fire():
    cfg = dict(D, max_entry_age_minutes=90, deployed_ttl_minutes=15)
    assert _expiry(cfg, leg_age=999, entry_age=999, deployed=True) == "max_entry_age"


def test_clean_staged_config_accepts_the_new_ttl_keys():
    out = S.clean_staged_config({"deployed_ttl_minutes": "15",
                                 "max_entry_age_minutes": 90})
    assert out == {"deployed_ttl_minutes": 15, "max_entry_age_minutes": 90}
    for bad in ({"deployed_ttl_minutes": -1}, {"max_entry_age_minutes": "abc"}):
        try:
            S.clean_staged_config(bad)
            assert False, f"expected reject for {bad}"
        except ValueError:
            pass


# ---- config validation (#129 Phase 1) ----
def test_clean_entry_style():
    assert S.clean_entry_style("STAGED") == "staged"
    assert S.clean_entry_style("limit") == "limit"
    for bad in ("staggered", "", None):
        try:
            S.clean_entry_style(bad)
            assert False, f"expected reject for {bad!r}"
        except ValueError:
            pass


def test_clean_staged_config_valid_and_coerced():
    out = S.clean_staged_config({"enabled": True, "deployed_ttl_minutes": "30"})
    assert out == {"enabled": True, "deployed_ttl_minutes": 30}
    assert S.clean_staged_config({"nonsense_key": 5}) is None      # unknown keys dropped
    assert S.clean_staged_config(None) is None


# The 13 that #250 deleted, along with the engine that read them. Named
# individually so this fails loudly if one is ever quietly reintroduced.
_DELETED_KNOBS = ("toe_in_tps", "runner_tps", "max_deferred_fraction",
                  "min_deferred_fraction", "reclaim_break_atr", "reclaim_break_cash",
                  "reclaim_break_max_frac_of_stop", "reclaim_break_abs_cap",
                  "stop_offset_atr", "runner_ttl_minutes",
                  "reclaim_pending_ttl_minutes", "reclaim_armed_ttl_minutes",
                  "min_stop_atr")


@pytest.mark.parametrize("knob", _DELETED_KNOBS)
def test_a_deleted_tuning_knob_is_dropped_never_stored(knob):
    """DROPPED, not rejected (#250). A saved row written by an older client still
    carries these; 422-ing them would make the whole strategy unsaveable, and the
    value would be ignored at read time anyway."""
    assert S.clean_staged_config({knob: 0.5}) is None
    assert S.clean_staged_config({"enabled": True, knob: 0.5}) == {"enabled": True}


@pytest.mark.parametrize("knob", _DELETED_KNOBS)
def test_a_deleted_knob_is_absent_from_the_effective_config(knob):
    assert knob not in S.staged_config({knob: 0.5})
    assert knob not in S.DEFAULT_STAGED


def test_clean_staged_config_rejects_bad_values():
    for bad in ({"enabled": "yes"},                    # bool required
                {"deployed_ttl_minutes": -1},          # negative
                {"max_entry_age_minutes": "abc"}):     # non-numeric
        try:
            S.clean_staged_config(bad)
            assert False, f"expected reject for {bad}"
        except ValueError:
            pass
    try:
        S.clean_staged_config([1, 2])                  # not an object
        assert False
    except ValueError:
        pass


def test_the_158_safety_bounds_stay_configurable():
    """Not tuning numbers — brakes. Off by default, and #250's list of 13 leaves
    them out on purpose."""
    out = S.clean_staged_config({"deployed_ttl_minutes": 30, "max_entry_age_minutes": 90})
    assert out == {"deployed_ttl_minutes": 30, "max_entry_age_minutes": 90}


def test_staged_config_overlay_completes_cfg():
    cfg = S.staged_config({"deployed_ttl_minutes": 30})
    assert cfg["deployed_ttl_minutes"] == 30           # override applied
    assert cfg["enabled"] == D["enabled"]              # default filled
    assert S.staged_config(None) == D                 # no stored -> pure defaults


