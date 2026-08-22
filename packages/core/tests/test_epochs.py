"""Rule epochs and the dark-arm alarm (#200).

The instrument these support (`filter_removed_set`, #186) accumulates a removed
set across weeks and tests it ONCE at N>=30. Everything here exists to make the
boundary of that accumulation a stored fact: a relabel must be free, a rule
change must not be, and an arm that has stopped trading must be noticed by the
system rather than by a human two days later.
"""
from pathlib import Path

from beacon_core.analysis import epochs as EP
from beacon_core.analysis import report as RP
from beacon_core.notifications import config as NC
from beacon_core.notifications import templates as NT

REPO_ROOT = Path(__file__).resolve().parents[3]

ADX = {"rules": [{"name": "skip_adx_trending_1h", "enabled": True,
                  "action": "skip",
                  "when": {"type": "adx_regime", "trending": True,
                           "timeframe": "1h"}}]}


def _with(**when):
    r = dict(ADX["rules"][0])
    r["when"] = {**r["when"], **when}
    return {"rules": [r]}


# --- the digest ---------------------------------------------------------------
def test_a_relabel_does_not_change_the_epoch():
    """The whole point. Renaming a rule cannot change what it removes, so it must
    not reset an accumulation — and `version`/`updated_at` bump either way, which
    is exactly why they could never be used for this."""
    renamed = {"rules": [{**ADX["rules"][0], "name": "arm_b_week_of_0810",
                          "note": "re-anchored for the new week"}]}
    assert EP.epoch_digest(ADX, None) == EP.epoch_digest(renamed, None)


def test_recording_where_a_rule_came_from_does_not_close_its_epoch():
    """#201 backfills a `provenance` block onto rules that are already running.
    Documenting a rule must never be the act that discards its accumulation —
    otherwise the backfill would close all six Arm-C epochs at once."""
    documented = {"rules": [{**ADX["rules"][0], "provenance": {
        "status": "recorded", "replay_run_id": 37,
        "effect_holdout": {"n": 11, "mean_r": -0.0363}}}]}
    assert EP.epoch_digest(ADX, None) == EP.epoch_digest(documented, None)


def test_a_rule_change_does_change_the_epoch():
    """`min_adx: 30` on 2026-08-06 closed a 52-removal accumulation. It has to
    read as a different experiment, because it is one."""
    assert EP.epoch_digest(ADX, None) != EP.epoch_digest(_with(min_adx=30), None)


def test_the_timeframe_is_part_of_the_epoch():
    # 4h -> 1h on 2026-07-30: the 4h half never fired at all.
    assert EP.epoch_digest(ADX, None) != EP.epoch_digest(_with(timeframe="4h"), None)


def test_shadow_and_live_are_different_experiments():
    """A rule flipped shadow->live starts actually skipping. The `when` block is
    untouched, so only `mode` distinguishes them — and a shadow rule removes
    nothing, so pooling the two describes a filter that never ran."""
    live = {"rules": [{**ADX["rules"][0], "mode": "live"}]}
    shadow = {"rules": [{**ADX["rules"][0], "mode": "shadow"}]}
    assert EP.epoch_digest(live, None) != EP.epoch_digest(shadow, None)


def test_disabling_a_rule_changes_the_epoch():
    off = {"rules": [{**ADX["rules"][0], "enabled": False}]}
    assert EP.epoch_digest(ADX, None) != EP.epoch_digest(off, None)


def test_the_same_number_written_three_ways_is_one_epoch():
    """The evaluator coerces all three (`execution/strategy._as_num`), so a UI
    that round-trips 30 as "30" must not silently orphan an accumulation."""
    a = EP.epoch_digest(_with(min_adx=30), None)
    b = EP.epoch_digest(_with(min_adx=30.0), None)
    c = EP.epoch_digest(_with(min_adx="30"), None)
    assert a == b == c


def test_key_order_is_not_information():
    reordered = {"rules": [{"when": {"timeframe": "1h", "trending": True,
                                     "type": "adx_regime"},
                            "action": "skip", "enabled": True,
                            "name": "skip_adx_trending_1h"}]}
    assert EP.epoch_digest(ADX, None) == EP.epoch_digest(reordered, None)


def test_the_digest_is_stable_across_processes():
    """Never Python's salted `hash()` — that would invent an epoch boundary on
    every restart, which is worse than having none at all."""
    assert EP.epoch_digest(ADX, None) == EP.epoch_digest(ADX, None)
    assert len(EP.epoch_digest(ADX, None)) == 16


def test_the_entry_policy_is_part_of_the_epoch():
    """Arm C's staged->single_shot restructure changed what the KEPT signals do,
    so pooling across it describes an arm that never existed."""
    staged = {"entry_style": "staged"}
    single = {"entry_style": "single_shot"}
    assert EP.epoch_digest(ADX, staged) != EP.epoch_digest(ADX, single)


def test_an_empty_filter_still_has_a_stable_epoch():
    # The control arm. It has an epoch too — it just never removes anything.
    assert EP.epoch_digest(None, None) == EP.epoch_digest({}, {})
    assert EP.epoch_digest({"rules": []}, None) == EP.epoch_digest({"rules": []}, {})


# --- the name -----------------------------------------------------------------
def test_the_epoch_name_is_derived_and_carries_its_digest():
    """The weekly script used to key removed sets off a hand-typed literal, which
    is how a missed bump mis-assigns a week of skips. The digest suffix makes the
    name an identity rather than a description."""
    name = EP.epoch_name(_with(min_adx=30), None)
    assert name.startswith("adx_regime@1h+min_adx30")
    assert name.endswith("#" + EP.epoch_digest(_with(min_adx=30), None)[:8])


def test_two_configurations_that_read_alike_still_get_different_names():
    a = EP.epoch_name(ADX, {"entry_style": "staged"})
    b = EP.epoch_name(ADX, {"entry_style": "single_shot"})
    assert a.split("#")[0] == b.split("#")[0] and a != b


def test_the_control_arm_is_named_not_blank():
    assert EP.epoch_name({"rules": []}, None).startswith("no_filter#")


# --- the dark arm -------------------------------------------------------------
def test_an_arm_skipping_everything_is_dark():
    out = EP.dark_arm(28, 28)
    assert out["dark"] and out["skip_rate"] == 1.0


def test_the_boundary_is_inclusive_on_both_bounds():
    """Exactly 10 signals at exactly 0.80 IS dark. An alarm with an ambiguous
    boundary gets argued with instead of acted on."""
    assert EP.dark_arm(10, 8)["dark"]
    assert not EP.dark_arm(10, 7)["dark"]          # 0.70
    assert not EP.dark_arm(9, 9)["dark"]           # one signal short of the floor


def test_a_quiet_day_is_not_a_dark_arm():
    """Below the floor a high rate is noise. Saying 'dark' on 2-of-2 would train
    the operator to ignore the alarm, which costs more than the missed day."""
    out = EP.dark_arm(2, 2)
    assert not out["dark"] and "below the 10 needed" in out["reason"]


def test_no_signals_at_all_is_not_dark():
    """A weekend is not a broken experiment, and 0/0 must not divide."""
    out = EP.dark_arm(0, 0)
    assert not out["dark"] and out["skip_rate"] is None


def test_the_thresholds_are_overridable_for_a_tighter_arm():
    assert EP.dark_arm(5, 5, min_signals=5, threshold=0.9)["dark"]


# --- the transition a write performs ------------------------------------------
OPEN = "2026-07-30T08:57:00Z"
NOW = "2026-08-10T08:23:00Z"


def test_a_relabel_leaves_the_clock_alone():
    m = EP.epoch_transition("abc", "abc", OPEN, NOW)
    assert m["started_at"] == OPEN and not m["closed"]


def test_a_rule_change_restarts_the_clock_and_says_so():
    m = EP.epoch_transition("abc", "def", OPEN, NOW)
    assert m["started_at"] == NOW and m["digest"] == "def" and m["closed"]


def test_a_row_that_predates_the_column_adopts_its_epoch_rather_than_resetting():
    """Every row on the box is unstamped on the deploy that adds the column. If
    the first write after that declared a new epoch, the whole book's
    accumulation would reset at once — the bug, applied universally."""
    m = EP.epoch_transition(None, "def", OPEN, NOW)
    assert m["started_at"] == OPEN and m["digest"] == "def" and not m["closed"]


# --- the closing note ---------------------------------------------------------
def test_the_close_note_states_the_evidence_being_discarded():
    """Stated as a loss at the moment of the decision. An epoch cannot be
    reopened, so the cost of the write IS the accumulation in flight."""
    note = EP.epoch_close_note(old_name="adx_regime@1h#abc",
                               started_at="2026-07-30T08:57:00Z",
                               n_skips=57, n_decisive=52, verdict="NO_EVIDENCE")
    assert "52/30" in note and "NO_EVIDENCE" in note and "cannot be reopened" in note


def test_the_close_note_says_short_when_the_epoch_dies_early():
    note = EP.epoch_close_note(old_name="e", started_at="2026-08-06T10:08:07Z",
                               n_skips=26, n_decisive=25, verdict="ACCUMULATE")
    assert "5 short of a verdict" in note


def test_the_close_note_works_without_a_decisive_count():
    """The API knows the skip count but not which account is the control, so it
    must be able to warn honestly with the number it actually has."""
    note = EP.epoch_close_note(old_name="e", started_at="2026-08-06T10:08:07Z",
                               n_skips=26)
    assert "26 accumulated skip(s)" in note


# --- the alarm is actually wired ----------------------------------------------
def test_arm_dark_is_routable_and_really_emitted():
    """`daily_summary` is the cautionary tale: routed, given an emoji, and never
    fired by anything, so an operator could write a template that never sends
    (#198). Adding a second one of those would be worse than adding no alarm."""
    assert "arm_dark" in NC.EVENT_IDS
    assert NT.is_emitted("arm_dark"), "arm_dark must carry a field contract"
    monitor = (REPO_ROOT / "services/monitor/main.py").read_text(encoding="utf-8")
    assert '_notify("arm_dark"' in monitor, "no service fires arm_dark"
    assert "await _check_dark_arms()" in monitor, "the check must run each tick"


def test_the_dark_arm_check_counts_signals_not_events():
    """One skipped signal fans out to several legs and each can log its own
    `entry_filtered`. Counting events would inflate the skip side against a
    trade side that is one-per-signal, and manufacture a dark arm out of a busy
    one — the same trap `filter_removed_set` documents."""
    monitor = (REPO_ROOT / "services/monitor/main.py").read_text(encoding="utf-8")
    body = monitor.split("async def _decisions_since(", 1)[1].split("\nasync def ", 1)[0]
    assert 'p.get("reason") != "filtration_skip"' in body   # de-sizes are not skips
    assert '["skipped"].add(sig)' in body and "len(v[\"skipped\"])" in body


# --- the stamp the event carries (#253) ---------------------------------------
def test_the_event_stamp_carries_both_the_join_and_the_group_key():
    """`epoch_digest` joins to `execution_strategies`; `epoch` is what
    `filter_removed_set` groups on. An event carrying only one of them still
    forces a reconstruction somewhere."""
    st = EP.event_stamp(ADX, None)
    assert st["epoch_digest"] == EP.epoch_digest(ADX, None)
    assert st["epoch"] == EP.epoch_name(ADX, None)
    assert st["epoch"].endswith(st["epoch_digest"][:8])


def test_the_stamp_separates_the_configuration_that_actually_ran():
    """The whole point: `min_adx: 30` on 2026-08-06 is a different experiment, so
    the skips it produced must not be able to land in the same bucket."""
    assert EP.event_stamp(ADX, None)["epoch"] != EP.event_stamp(_with(min_adx=30), None)["epoch"]


def test_the_stamp_is_computed_from_the_rules_not_read_off_the_row():
    """`event_stamp` deliberately takes no stored digest. Every config act since
    2026-08-17 was applied as direct SQL, which leaves the row's `epoch_digest`
    describing the PREVIOUS rules — and stamping that onto the new rules' skips
    is exactly the mis-assignment #253 is about, with a stored fact to back it."""
    import inspect
    assert "digest" not in inspect.signature(EP.event_stamp).parameters


def test_the_executor_stamps_every_filtration_event():
    """A stamp that exists only in `epochs.py` is a stamp nobody carries. The
    filtration_skip event is the one `filter_removed_set` reads, so it is named
    separately from the de-size events."""
    ex = (REPO_ROOT / "services/executor/main.py").read_text(encoding="utf-8")
    assert "_epoch = EP.event_stamp(" in ex
    assert "ST.entry_filters_row(_chain)" in ex, (
        "the epoch must come from the row that SUPPLIED the filters")
    skip = ex.split('"reason": "filtration_skip"', 1)[0].rsplit(
        'kind="entry_filtered"', 1)[1]
    assert "**_epoch" in skip, "filtration_skip must carry the epoch it ran under"


def test_a_rule_edit_mid_accumulation_produces_two_epochs_not_one():
    """The near-miss, end to end. Nineteen skips under one `adx_regime` rule and
    nineteen more after `min_adx: 30` is added: stamped at emit time they are two
    epochs, each below the N>=30 floor and therefore ACCUMULATE. Pooled — which
    is what a hand-derived epoch literal does — they clear the floor and the arm
    gets a verdict it never earned on any configuration it ever ran."""
    before, after = EP.event_stamp(ADX, None), EP.event_stamp(_with(min_adx=30), None)
    skips, control = [], []
    for i in range(38):
        stamp = before if i < 19 else after
        skips.append({"signal_id": i, "epoch": stamp["epoch"]})
        # removed set loses; the kept set (ids 100+) wins.
        control.append({"signal_id": i, "realized_pl": -100.0, "planned_risk": 100.0,
                        "day": f"2026-08-{(i % 5) + 1:02d}", "source_id": i % 3})
    for i in range(100, 140):
        control.append({"signal_id": i, "realized_pl": 50.0, "planned_risk": 100.0,
                        "day": f"2026-08-{(i % 5) + 1:02d}", "source_id": i % 3})

    out = RP.filter_removed_set(skips, control, base_rate=0.5)
    assert out["n_epochs"] == 2, out["epochs"].keys()
    assert set(out["epochs"]) == {before["epoch"], after["epoch"]}
    for e in out["epochs"].values():
        assert e["n_skipped"] == 19
        assert e["verdict"] == RP.FILTER_ACCUMULATE   # 19 < MIN_REMOVED_N

    # Pooled under a single hand-written label the same 38 skips clear the floor.
    pooled = RP.filter_removed_set(
        [{"signal_id": s["signal_id"], "epoch": "adx_regime@1h"} for s in skips],
        control, base_rate=0.5)
    assert pooled["n_epochs"] == 1
    assert pooled["epochs"]["adx_regime@1h"]["verdict"] != RP.FILTER_ACCUMULATE
