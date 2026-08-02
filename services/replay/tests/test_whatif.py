"""The what-if module (#183 rebuild).

The screen exists to answer one question — "would we have made money doing it
differently?" — so what is tested here is that the answer is CORRECT and that it
is READABLE. Both matter: a report that quietly answers a different question than
the one asked is worse than one that errors, because nothing surfaces it.
"""
from __future__ import annotations

import copy
import datetime as dt

import pytest

from harness import whatif as W


# --- the change vocabulary ----------------------------------------------------
def test_every_named_exit_resolves_to_rules_that_can_actually_fire():
    for name, rules in W.EXITS.items():
        assert rules, f"{name} resolved to an empty ladder"
        for r in rules:
            assert "trigger" in r and "action" in r, name


def test_let_it_run_is_a_rule_that_never_fires_not_an_empty_list():
    """An EMPTY sl_rules list reads as UNSET and cascades to the DEFAULT ladder,
    so "never move the stop" expressed as `[]` would silently be BE@TP1 — the
    report would then compare the baseline against itself and say "no
    difference", which is a wrong answer rather than an error."""
    assert W.EXITS["let_it_run"][0]["trigger"]["index"] == 99


def test_every_exit_has_a_label_a_person_can_read():
    assert set(W.EXIT_LABELS) == set(W.EXITS)
    for label in W.EXIT_LABELS.values():
        # Read mid-sentence ("What-if: move stop to breakeven at TP1"), so the
        # first word is lowercase. Acronyms inside it stay upper.
        assert label[0].islower(), label


def test_an_rsi_ceiling_is_stated_as_what_to_KEEP():
    """The operator says what must be true to take the trade; the skip is derived.

    Stating each condition as its own skip rule would mean inverting every
    operator by hand, and inverting `between` or a boolean field is exactly how
    you end up filtering the wrong half of the book."""
    leaf = W.keep_leaf({"kind": "indicator", "id": "rsi", "field": "value",
                        "op": "lt", "value": 70, "timeframe": "15m"})
    assert leaf == {"type": "indicator", "id": "rsi", "timeframe": "15m",
                    "field": "value", "op": "lt", "value": 70}


def test_a_boolean_condition_carries_no_value():
    """`is_true` on a field like `fvg.present` takes no bound. Sending one would
    be read as a numeric compare against a boolean and evaluate to UNKNOWN."""
    leaf = W.keep_leaf({"kind": "indicator", "id": "fvg", "field": "present",
                        "op": "is_true", "timeframe": "15m"})
    assert "value" not in leaf and leaf["op"] == "is_true"


def test_a_condition_can_compare_against_the_price():
    """What makes "price is above the order block" expressible without a bespoke
    rule type: the right-hand side is a reference, not a literal."""
    leaf = W.keep_leaf({"kind": "indicator", "id": "order_block", "field": "top",
                        "op": "lt", "ref": "price", "timeframe": "15m"})
    assert leaf["ref"] == "price" and "value" not in leaf


def test_structure_indicators_are_reachable_because_they_are_just_indicators():
    """FVG and order blocks are registry entries like any other, so the builder
    reaches them through the same leaf — there is no special case to maintain."""
    for ind, field in (("fvg", "present"), ("order_block", "dist_pct"),
                       ("support_resistance", "dist_support_pct"),
                       ("bbands", "pct_b"), ("macd", "cross")):
        leaf = W.keep_leaf({"kind": "indicator", "id": ind, "field": field,
                            "op": "gt", "value": 0})
        assert leaf["id"] == ind and leaf["field"] == field


def test_the_regime_and_session_conditions_still_exist():
    assert W.keep_leaf({"kind": "regime", "trending": True})["trending"] is True
    assert W.keep_leaf({"kind": "session", "sessions": ["London"]}) == {
        "type": "session_in", "sessions": ["London"]}


def test_an_unknown_condition_kind_is_dropped_not_guessed():
    assert W.keep_leaf({"kind": "vibes"}) is None
    assert W.keep_leaf({"kind": "indicator", "id": "rsi"}) is None      # no op


def test_every_condition_folds_into_ONE_skip_on_their_negation():
    """The composition that makes an arbitrary number of conditions safe.

    Kleene: `not UNKNOWN` is UNKNOWN, so a condition whose indicator could not be
    computed leaves the whole rule UNKNOWN, which reads as "does not match" and
    the signal is TAKEN. Fail-open survives composition, which is the entire
    reason the grammar is three-valued."""
    rules = W.entry_rules({"conditions": [
        {"kind": "indicator", "id": "rsi", "field": "value", "op": "lt", "value": 60},
        {"kind": "regime", "trending": True}]})
    assert len(rules) == 1
    r = rules[0]
    assert r["action"] == "skip" and r["mode"] == "live" and r["enabled"] is True
    assert list(r["when"]) == ["not"]
    assert len(r["when"]["not"]["all"]) == 2


def test_a_skip_session_preset_becomes_a_negated_keep():
    """It is the one condition stated as a skip, so it inverts on the way in."""
    rules = W.entry_rules({"filters": [{"kind": "skip_session",
                                        "sessions": ["New York"]}]})
    leaf = rules[0]["when"]["not"]["all"][0]
    assert leaf == {"not": {"type": "session_in", "sessions": ["New York"]}}


def test_no_conditions_means_no_rule_at_all():
    """An empty rule list leaves the arm's filtration untouched. A rule with an
    empty `all` would be UNKNOWN and inert, but shipping one invites the reader
    to think something was applied."""
    assert W.entry_rules({}) == []
    assert W.entry_rules({"filters": [{"kind": "min_stop_atr", "value": 1.0}]}) == []


def test_a_geometry_filter_never_becomes_an_engine_rule():
    """There is no `when.type` for stop distance, so it is applied on the signal
    set instead — and must not silently vanish into the condition list."""
    assert W.conditions_of({"filters": [{"kind": "min_stop_atr", "value": 1.0}]}) == []
    assert "min_stop_atr" in W.GEOMETRY_KINDS


class _Parsed:
    def __init__(self, entry_to, sl):
        self.entry_to, self.sl = entry_to, sl


class _Sig:
    def __init__(self, entry_to, sl):
        self.parsed = _Parsed(entry_to, sl)


def test_the_atr_filter_skips_a_tight_stop_and_keeps_a_wide_one():
    f = {"kind": "min_stop_atr", "value": 1.0}
    assert W.geometry_skip(f, _Sig(2000, 1997), 5.0) is True      # 0.6x ATR
    assert W.geometry_skip(f, _Sig(2000, 1993), 5.0) is False     # 1.4x ATR


def test_a_missing_atr_keeps_the_signal_rather_than_dropping_it():
    """Fail-open. A thin series at the start of the window has no ATR, and
    silently skipping every one of those signals would make the what-if arm
    look brilliant for a reason that has nothing to do with the filter."""
    f = {"kind": "min_stop_atr", "value": 1.0}
    assert W.geometry_skip(f, _Sig(2000, 1997), None) is False
    assert W.geometry_skip(f, _Sig(2000, 1997), 0) is False


# --- how the change is applied ------------------------------------------------
def _live():
    return {"name": "live", "strategies": [
        {"account_id": None, "source_id": None,
         "entry_filters": {"rules": [{"name": "pre-existing", "enabled": True}]},
         "exit_policy": {"sl_rules": [{"trigger": {"type": "tp_hit", "index": 1},
                                       "action": {"type": "move_sl_to",
                                                  "target": "entry"}}]}},
        {"account_id": 2, "source_id": 7,
         "exit_policy": {"sl_rules": [{"trigger": {"type": "tp_hit", "index": 3},
                                       "action": {"type": "move_sl_to",
                                                  "target": "entry"}}]}}]}


def test_a_filter_replaces_what_was_there_rather_than_stacking_on_it():
    """The operator is asking "what if we filtered by THIS", not "what if we
    added it on top of whatever is already configured and cannot see from this
    screen"."""
    out = W.apply_changes(_live(), {"conditions": [{"kind": "regime",
                                                    "trending": True}]})
    rules = out["strategies"][0]["entry_filters"]["rules"]
    assert len(rules) == 1
    assert rules[0]["when"]["not"]["all"][0]["type"] == "adx_regime"


def test_a_free_form_ladder_reaches_the_arm():
    out = W.apply_changes(_live(), {"exit_steps": [
        {"when": {"kind": "points", "points": 30},
         "then": {"kind": "breakeven"}}]})
    assert out["strategies"][0]["exit_policy"]["sl_rules"] == [
        {"trigger": {"type": "price_move", "points": 30.0},
         "action": {"type": "move_sl_to", "target": "entry"}}]


def test_a_scoped_override_cannot_survive_a_change_to_the_base_layer():
    """The per-(account, source) layer wins over the base one at runtime. Leaving
    it in place while changing the base means the arm silently keeps the old
    exit for exactly the channel the operator is testing."""
    out = W.apply_changes(_live(), {"exit": "be_at_tp2"})
    base, scoped = out["strategies"]
    assert base["exit_policy"]["sl_rules"] == W.EXITS["be_at_tp2"]
    assert "sl_rules" not in scoped["exit_policy"]


def test_the_live_config_is_never_mutated():
    """Both arms are built from the same object; mutating it in place would make
    the baseline the what-if and the comparison meaningless."""
    live = _live()
    before = copy.deepcopy(live)
    W.apply_changes(live, {"exit": "let_it_run",
                           "filters": [{"kind": "only_ranging"}],
                           "risk_percent": 0.5})
    assert live == before


def test_risk_is_expressed_in_the_shape_the_sizer_reads():
    out = W.apply_changes(_live(), {"risk_percent": 0.75})
    assert out["risk"]["default"] == {"basis": "capital_percent", "value": 0.75,
                                      "allocation": "even"}


def test_no_change_leaves_the_variant_alone():
    live = _live()
    assert W.apply_changes(live, {}) == live


# --- describing it ------------------------------------------------------------
def test_the_change_reads_as_a_sentence_not_a_config():
    """With a free-form builder in front of it, this is the only thing standing
    between the operator and a column headed with a JSON blob."""
    assert W.describe({"conditions": [
        {"kind": "indicator", "id": "rsi", "field": "value", "op": "lt",
         "value": 70, "timeframe": "15m"}]}) ==         "only trade when RSI on 15m is below 70"
    assert W.describe({"exit": "be_at_tp1"}) == "move stop to breakeven at TP1"
    assert W.describe({}) == "no change"


def test_several_conditions_read_as_one_sentence():
    text = W.describe({"conditions": [
        {"kind": "indicator", "id": "fvg", "field": "present", "op": "is_true",
         "timeframe": "15m"},
        {"kind": "regime", "trending": True}]})
    assert text == ("only trade when Fair Value Gap is there on 15m "
                    "and the market is trending")


def test_a_structure_field_is_named_in_the_sentence():
    text = W.describe({"conditions": [
        {"kind": "indicator", "id": "order_block", "field": "dist_pct",
         "op": "lte", "value": 0.5, "timeframe": "15m"}]})
    assert text == "only trade when Order Block dist pct on 15m is at or below 0.5"


def test_a_built_ladder_reads_as_steps():
    text = W.describe({"exit_steps": [
        {"when": {"kind": "points", "points": 30}, "then": {"kind": "breakeven"}},
        {"when": {"kind": "tp", "index": 2}, "then": {"kind": "previous_tp"}}]})
    assert text == ("when price moves 30 in our favour, move the stop to breakeven"
                    " then when TP2 is hit, move the stop to the previous target")


def test_an_r_trigger_reads_in_r():
    assert "profit reaches 1.5R" in W.describe(
        {"exit_steps": [{"when": {"kind": "r", "r": 1.5},
                         "then": {"kind": "breakeven"}}]})


def test_a_session_skip_names_the_session():
    assert "New York" in W.describe(
        {"filters": [{"kind": "skip_session", "sessions": ["New York"]}]})


# --- reading the outcome ------------------------------------------------------
class _Leg:
    def __init__(self, outcome=None, tp_index=None, fill_price=None, entry=None):
        self.outcome, self.tp_index = outcome, tp_index
        self.fill_price, self.entry = fill_price, entry


_NEXT_SIG = [0]


class _Trade:
    def __init__(self, *, pl, mfe, entry=2000, sl=1990, direction="BUY",
                 legs=None, filled=True, signal_id=None, account_id=1):
        self.realized_pl, self.mfe = pl, mfe
        self.initial_sl, self.direction, self.ever_filled = sl, direction, filled
        self.legs = legs if legs is not None else [_Leg(fill_price=entry)]
        if signal_id is None:
            _NEXT_SIG[0] += 1
            signal_id = _NEXT_SIG[0]
        self.signal_id, self.account_id = signal_id, account_id


class _Res:
    def __init__(self, trades, not_taken=()):
        self.trades, self.not_taken = list(trades), list(not_taken)


def test_travel_separates_an_entry_problem_from_an_exit_problem():
    """The three shapes a person actually asks about. A loser that never moved
    0.3R our way is a bad ENTRY; one that got a full R and gave it back is a bad
    EXIT. Collapsing both into "loss" is what makes a losing month unreadable."""
    assert W._travel(_Trade(pl=-100, mfe=2002)) == "straight_to_sl"
    assert W._travel(_Trade(pl=-100, mfe=2006)) == "ranged"
    assert W._travel(_Trade(pl=-100, mfe=2012)) == "went_our_way_then_reversed"
    assert W._travel(_Trade(pl=300, mfe=2012)) == "ran_to_target"


def test_travel_is_measured_in_the_direction_of_the_trade():
    """On a SELL the favourable excursion is DOWN. Reading it as up would label
    every short that worked as "straight to SL"."""
    t = _Trade(pl=300, mfe=1988, entry=2000, sl=2010, direction="SELL")
    assert W._travel(t) == "ran_to_target"


def test_travel_says_unknown_rather_than_guessing():
    assert W._travel(_Trade(pl=0, mfe=None)) == "unknown"
    assert W._travel(_Trade(pl=0, mfe=2010, entry=2000, sl=2000)) == "unknown"


def test_the_summary_counts_what_the_operator_asked_to_see():
    res = _Res(
        trades=[
            _Trade(signal_id=1, pl=120, mfe=2012,
                   legs=[_Leg("tp_hit", 1, fill_price=2000),
                         _Leg("tp_hit", 2, fill_price=2000)]),
            _Trade(signal_id=2, pl=-80, mfe=2002,
                   legs=[_Leg("sl_hit", fill_price=2000)]),
            _Trade(signal_id=3, pl=0, mfe=None, filled=False, legs=[_Leg()]),
        ],
        not_taken=[{"signal_id": 4, "reason": "filtration:RSI at or above 70"},
                   {"signal_id": 5, "reason": "unknown_account"}])
    s = W.summarise(res, label="x")
    assert s["signals"] == 5                 # 3 simulated + 2 never simulated
    assert s["executed"] == 2
    assert s["skipped"] == 3                 # 2 not taken + 1 never filled
    assert s["skipped_by_rule"] == 1
    assert s["skipped_no_fill"] == 1
    assert s["skipped_other"] == 1
    assert s["profit"] == 40.0
    assert (s["wins"], s["losses"]) == (1, 1)
    assert (s["tp1"], s["tp2"], s["tp3"]) == (1, 1, 0)
    assert s["stopped_out"] == 1
    assert s["travel"] == {"ran_to_target": 1, "straight_to_sl": 1}


def test_one_signal_on_three_accounts_is_one_signal():
    """THE BUG THIS EXISTS FOR. The simulator emits one row per (signal,
    account), and this book fans one signal out to three accounts — so summing
    its rows reported 492 signals for a channel that sent 170. The caveat line
    directly underneath said 170, which is how it was caught.

    Money is the exception: it stays a total across accounts, because "would
    that have made us profitable" is a question about the book."""
    res = _Res(trades=[
        _Trade(signal_id=1, account_id=a, pl=50, mfe=2012,
               legs=[_Leg("tp_hit", 1, fill_price=2000)]) for a in (5, 7, 8)])
    s = W.summarise(res, label="x")
    assert s["signals"] == 1 and s["executed"] == 1 and s["wins"] == 1
    assert s["tp1"] == 1
    assert s["profit"] == 150.0            # the book, not one account's share


def test_a_signal_filled_on_one_account_and_blocked_on_another_was_traded():
    """A risk cap turning one account away does not make the signal a skip —
    it was traded. Counting it in both columns would double it."""
    res = _Res(
        trades=[_Trade(signal_id=1, account_id=5, pl=50, mfe=2012,
                       legs=[_Leg("tp_hit", 1, fill_price=2000)])],
        not_taken=[{"signal_id": 1, "reason": "risk_blocked"}])
    s = W.summarise(res, label="x")
    assert s["signals"] == 1 and s["executed"] == 1 and s["skipped"] == 0


def test_the_ladder_is_counted_per_signal_not_per_leg():
    """A staged entry is several legs on ONE signal. Counting legs reported 172
    stop-outs against 78 executed trades — a number that cannot be read sitting
    next to one that can.

    Cumulative, because that is how a ladder is read out loud: a signal that
    reached TP2 also reached TP1."""
    t = _Trade(pl=50, mfe=2012, legs=[_Leg("tp_hit", 1, fill_price=2000),
                                      _Leg("tp_hit", 1, fill_price=2000),
                                      _Leg("tp_hit", 2, fill_price=2000)])
    s = W.summarise(_Res([t]), label="x")
    assert (s["tp1"], s["tp2"], s["tp3"]) == (1, 1, 0)


def test_a_signal_that_banked_tp1_then_stopped_is_not_a_stop_out():
    """"Stopped out" has to mean it reached NO target. A runner stopped at
    breakeven after TP1 is a different outcome, and lumping them together is
    what makes the exit question unanswerable."""
    banked = _Trade(pl=40, mfe=2012, legs=[_Leg("tp_hit", 1, fill_price=2000),
                                           _Leg("breakeven", fill_price=2000)])
    pure = _Trade(pl=-80, mfe=2002, legs=[_Leg("sl_hit", fill_price=2000),
                                          _Leg("sl_hit", fill_price=2000)])
    s = W.summarise(_Res([banked, pure]), label="x")
    assert s["stopped_out"] == 1


def test_a_geometry_skip_is_counted_as_a_rule_skip_not_as_an_error():
    """The worker declares them with a `whatif:` reason because the engine has no
    filter to attribute them to. If `summarise` did not recognise the prefix they
    would land in "other", and the operator would see a filter that removed
    nothing while the numbers moved."""
    s = W.summarise(_Res([], [{"signal_id": 1, "reason": "whatif:min_stop_atr"}]),
                    label="x")
    assert s["skipped_by_rule"] == 1 and s["skipped_other"] == 0


# --- the verdict --------------------------------------------------------------
def _sum(profit, *, wins=5, by_rule=0, executed=20):
    return {"profit": profit, "wins": wins, "skipped_by_rule": by_rule,
            "executed": executed}


def test_the_verdict_says_better_or_worse_in_money():
    v = W.verdict(_sum(-3238), _sum(0, wins=1, by_rule=26),
                  {"filters": [{"kind": "rsi_below", "value": 70}]})
    assert v["better"] is True and v["delta"] == 3238
    assert "3,238" in v["headline"] and "Better" in v["headline"]
    assert "26 signal" in v["headline"]
    assert "4 of them were winners" in v["headline"]
    assert v["change"] == "only trade when RSI on 15m is below 70"


def test_a_worse_result_is_not_dressed_up():
    v = W.verdict(_sum(500), _sum(100), {"exit": "be_at_tp1"})
    assert v["better"] is False and v["delta"] == -400
    assert "Worse by 400" in v["headline"]


def test_an_identical_result_says_so():
    assert W.verdict(_sum(100), _sum(100), {})["headline"].startswith("No difference")


def test_a_filter_that_skips_everything_says_it_is_untestable():
    """Otherwise it reads as "0 lost instead of 3,238 lost — huge win", which is
    the single most misleading thing this page could say."""
    v = W.verdict(_sum(-3238), _sum(0, executed=0, by_rule=100), {})
    assert "skipped EVERYTHING" in v["headline"]


def test_a_thin_result_is_labelled_a_hint():
    v = W.verdict(_sum(-500), _sum(200, executed=4), {})
    assert "hint" in v["headline"]


def test_a_filter_that_barely_applied_says_so_instead_of_reporting_the_delta():
    """MEASURED, not hypothetical: an RSI-below-70 filter touched 2 of 114
    Quartz Elite signals, because RSI is rarely that high when these channels
    post. The delta was +80 on a -1,189 book. Reporting "Better by 79.91" and
    stopping there invites acting on noise."""
    v = W.verdict(_sum(-1189, by_rule=0), _sum(-1109, by_rule=0),
                  {"filters": [{"kind": "rsi_below", "value": 70}]})
    assert "barely applied" in v["headline"]


def test_a_filter_that_removed_nearly_everything_says_that_too():
    v = W.verdict(_sum(-1000, executed=100, by_rule=0),
                  _sum(50, executed=5, by_rule=95),
                  {"filters": [{"kind": "only_trending"}]})
    assert "nearly everything" in v["headline"]


# --- the caveat -------------------------------------------------------------
class _At:
    def __init__(self, at):
        self.at = at


def _burst(n, when, spacing=1):
    return [_At(when + dt.timedelta(seconds=i * spacing)) for i in range(n)]


def test_a_backfilled_burst_is_declared_even_when_nothing_time_dependent_changed():
    """MEASURED: 179 of 856 signals sit in the single 15-minute window
    2026-07-05 16:30, and NONE of those 179 produced a real trade. They are the
    onboarding backlog — imported at once, never executed.

    This caveat is about the LEFT column, so it fires whatever was changed: both
    arms simulate those signals, and the operator has to know the baseline is
    not a replay of their account statement."""
    sigs = _burst(30, dt.datetime(2026, 7, 5, 16, 32)) + \
        [_At(dt.datetime(2026, 7, 8, 9, 0)), _At(dt.datetime(2026, 7, 9, 11, 0))]
    for changes in ({"exit": "be_at_tp1"}, {"risk_percent": 1.0}):
        cs = W.bulk_ingest_caveats(sigs, changes)
        assert len(cs) == 1
        assert "2026-07-05 16:30" in cs[0] and "SIMULATE" in cs[0]


def test_a_filter_adds_the_second_caveat_because_it_cannot_see_the_block():
    """A filter reads the market at the signal's timestamp, so for a burst it
    reads ONE moment. Unsaid, that reads as "the RSI filter barely did anything"
    when the truth is "a fifth of the history has no usable time to read an
    indicator at".

    An exit ladder is evaluated bar by bar AFTER entry, so it is NOT blinded the
    same way — adding this line there would train the operator to ignore it."""
    sigs = _burst(30, dt.datetime(2026, 7, 5, 16, 32))
    cs = W.bulk_ingest_caveats(sigs, {"filters": [{"kind": "rsi_below", "value": 70}]})
    assert len(cs) == 2
    assert "unmeasured" in cs[1]
    assert len(W.bulk_ingest_caveats(sigs, {"exit": "be_at_tp1"})) == 1


def test_evenly_spread_signals_raise_no_caveat():
    sigs = [_At(dt.datetime(2026, 7, 5, 9, 0) + dt.timedelta(hours=3 * i))
            for i in range(40)]
    assert W.bulk_ingest_caveats(
        sigs, {"filters": [{"kind": "only_trending"}]}) == []


def test_the_report_carries_the_caveat_where_the_page_can_show_it():
    sigs = _burst(30, dt.datetime(2026, 7, 5, 16, 32))
    rep = W.report(_Res([]), _Res([]),
                   changes={"filters": [{"kind": "rsi_below", "value": 70}]},
                   scope_label="Quartz Elite", signals=sigs)
    assert len(rep["caveats"]) == 2
    assert "backlog" in rep["caveats"][0]


class _S:
    def __init__(self, n_tps, at=None):
        self.parsed = type("P", (), {"tps": [0] * n_tps})()
        self.at = at or dt.datetime(2026, 7, 8, 9, 0)


def test_an_exit_that_can_never_fire_is_declared():
    """MEASURED: all 114 Quartz Elite signals post exactly 2 targets, so
    `be_at_tp2` and `let_it_run` returned BYTE-IDENTICAL results — TP2 closes
    the last leg and there is nothing left to move a stop on.

    The run was really measuring "stop ratcheting at TP1", and the verdict named
    the wrong cause. A change that cannot fire is the silent-no-op failure this
    module exists to refuse: nothing errors, the numbers move for another
    reason, and the operator acts on a wrong attribution."""
    c = W.exit_reach_caveat([_S(2) for _ in range(114)], {"exit": "be_at_tp2"})
    assert c and c.startswith("Every one of these 114 signals")
    assert "REMOVING the exit you run today" in c


def test_a_ladder_deep_enough_for_the_ratchet_raises_nothing():
    assert W.exit_reach_caveat([_S(4) for _ in range(50)],
                               {"exit": "be_at_tp2"}) is None
    assert W.exit_reach_caveat([_S(2) for _ in range(50)],
                               {"exit": "be_at_tp1"}) is None


def test_never_moving_the_stop_is_always_reachable():
    """`let_it_run` is the absence of a ratchet, so it cannot fail to fire."""
    assert W.exit_reach_caveat([_S(2) for _ in range(50)],
                               {"exit": "let_it_run"}) is None


def test_a_mixed_book_below_the_threshold_raises_nothing():
    """Half the book reaching TP2 is a real test, not a no-op."""
    sigs = [_S(2) for _ in range(50)] + [_S(4) for _ in range(50)]
    assert W.exit_reach_caveat(sigs, {"exit": "be_at_tp2"}) is None


def test_the_unreachable_exit_is_the_first_thing_the_report_says():
    """It changes what the numbers MEAN, so it outranks the backfill note."""
    sigs = [_S(2, dt.datetime(2026, 7, 5, 16, 32)) for _ in range(30)]
    rep = W.report(_Res([]), _Res([]), changes={"exit": "be_at_tp2"},
                   scope_label="Quartz Elite", signals=sigs)
    assert len(rep["caveats"]) == 2
    assert "never has a leg left to protect" in rep["caveats"][0]


def test_a_simulated_profit_on_a_losing_channel_is_called_out():
    """MEASURED: GOLD VIP SIGNAL TM simulates at +1,438 over 2026-07-05..07-31
    while the account really lost 41,639 on it in the same window (113 trades).

    The simulated number is not wrong for what it is — today's config over old
    signals, and the book ran 5% risk until 2026-07-25 against today's 2% — but
    read on its own it turns the worst channel on the book into a winner. That
    is the single most expensive misreading this page could invite."""
    c = W.reality_caveat({"trades": 113, "profit": -41638.72},
                         {"profit": 1438.21})
    assert c and c.startswith("The simulated column says you MADE money")
    assert "-41,638.72" in c and "113 trades" in c
    assert "CURRENT settings over old signals" in c


def test_the_mirror_case_is_called_out_too():
    c = W.reality_caveat({"trades": 40, "profit": 5000.0}, {"profit": -900.0})
    assert c and "LOST money on a window where the account made it" in c


def test_agreement_raises_nothing():
    assert W.reality_caveat({"trades": 40, "profit": -1200.0},
                            {"profit": -1100.0}) is None


def test_a_big_gap_in_the_same_direction_is_still_called_out():
    """Same sign is not agreement. Losing 40k and simulating a 1k loss is the
    same misreading wearing a minus sign."""
    c = W.reality_caveat({"trades": 113, "profit": -41638.72},
                         {"profit": -1100.0})
    assert c and "a long way from what the account did" in c


def test_a_window_with_no_real_trades_says_nothing():
    """A backtest over a period the account never traded has nothing to
    compare against — a warning there would be noise."""
    assert W.reality_caveat({"trades": 0, "profit": 0.0}, {"profit": 500.0}) is None
    assert W.reality_caveat(None, {"profit": 500.0}) is None


def test_the_reality_gap_outranks_every_other_caveat():
    """It decides whether the left column means anything at all."""
    sigs = [_S(2, dt.datetime(2026, 7, 5, 16, 32)) for _ in range(30)]
    rep = W.report(_Res([]), _Res([]), changes={"exit": "be_at_tp2"},
                   scope_label="GOLD VIP SIGNAL TM", signals=sigs,
                   actual={"trades": 113, "profit": -41638.72})
    assert len(rep["caveats"]) == 3
    assert "Really traded:" in rep["caveats"][0]
    assert rep["actual"]["trades"] == 113


def test_the_baseline_is_not_called_what_happened():
    """It is a SIMULATION of the current setup over these signals. On this book
    179 of 856 signals were never traded live at all, and the Reconciler exists
    because sim and broker truth differ anyway (agreement 0.9149, #187).
    "What happened" put a claim on that column the number cannot support."""
    rep = W.report(_Res([]), _Res([]), changes={"exit": "be_at_tp1"},
                   scope_label="x")
    assert rep["baseline"]["label"] == "Your setup now"


def test_the_report_carries_both_arms_and_the_window():
    frm, to = dt.datetime(2026, 7, 1), dt.datetime(2026, 7, 30)
    rep = W.report(_Res([]), _Res([]), changes={"exit": "be_at_tp1"},
                   scope_label="Quartz Elite", frm=frm, to=to)
    assert rep["scope"] == "Quartz Elite"
    assert rep["from"].startswith("2026-07-01") and rep["to"].startswith("2026-07-30")
    assert rep["change"] == "move stop to breakeven at TP1"
    assert set(rep) >= {"baseline", "whatif", "verdict", "note"}
    # The screening caveat is not optional: this is not what promotes a config.
    assert "A/B" in rep["note"]


@pytest.mark.parametrize("kind", sorted(
    {"rsi_below", "rsi_above", "only_trending", "only_ranging", "skip_session",
     "in_fvg", "at_order_block", "min_stop_atr"}))
def test_every_preset_resolves_to_a_condition_or_a_geometry_skip(kind):
    """A preset with nothing behind it is a silent no-op: the arm runs unchanged
    and the report says the change made no difference."""
    f = {"kind": kind, "value": 1.0, "sessions": ["New York"]}
    assert W.preset_leaf(f) is not None or kind in W.GEOMETRY_KINDS
    assert W.describe({"filters": [f]}) != kind, "every preset needs a phrase"


def test_every_preset_names_an_indicator_the_registry_actually_carries():
    """The `macd.cross == "bull"` failure, generalised: an id or field the
    registry does not carry evaluates to UNKNOWN on every bar, so the condition
    silently never holds and the arm is the baseline wearing a new label."""
    from beacon_core.ta import registry as TA
    specs = {s["id"]: s for s in TA.catalog()["indicators"]}
    for kind in W.PRESETS:
        leaf = W.preset_leaf({"kind": kind, "value": 1.0})
        if leaf.get("kind") != "indicator":
            continue
        spec = specs.get(leaf["id"])
        assert spec, f"{kind} names an indicator that is not in the registry"
        assert leaf["field"] in spec["outputs"], (
            f"{kind} reads {leaf['field']}, but {leaf['id']} emits "
            f"{spec['outputs']}")
