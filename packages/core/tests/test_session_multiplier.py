"""What a session `risk_mult` is actually doing (#270).

`settings.trading_hours` lets a session halve our size, and one does: newyork
0.5, set 2026-07-16 and never evaluated. The engine had no surface that reported
what a multiplier was DOING, so the belief behind it went six weeks unexamined
while our own exit-independent label said the halved cohort hits TP1 MORE often.

Everything here is pure: dict rows in, verdict out. `session_multiplier_report`
is measure-only (CLAUDE.md 2, shadow-first) - no config moves off it.
"""
from beacon_core.analysis import report as RP


def _rows(day_count, *, desized_per_day, full_per_day, desized_wins,
          full_wins, hour=12, full_hour=None, session="newyork", factor=0.5):
    """A synthetic book: `desized_wins` of each day's de-sized signals hit TP1
    first, `full_wins` of the full-size ones do."""
    out = []
    for d in range(1, day_count + 1):
        day = f"2026-08-{d:02d}"
        for i in range(desized_per_day):
            out.append({"tp1_first": i < desized_wins, "day": day, "hour": hour,
                        "session": session, "factor": factor})
        for i in range(full_per_day):
            out.append({"tp1_first": i < full_wins, "day": day,
                        "hour": full_hour if full_hour is not None else hour,
                        "session": None, "factor": None})
    return out


def test_a_session_that_never_desized_is_untested_not_null():
    """A multiplier that never fired has not been measured, and calling that
    NO_EVIDENCE would let an unexamined default read as a tested one."""
    rows = [{"tp1_first": True, "day": "2026-08-01", "hour": 12,
             "session": None, "factor": None} for _ in range(200)]
    out = RP.session_multiplier_report(rows)
    assert out["sessions"] == {} and out["n_groups"] == 0


def test_a_multiplier_of_one_is_not_a_contrast():
    """`risk_mult: 1.0` multiplies by one. Letting it form a group would
    manufacture a result out of a no-op - and both london and asian carry it."""
    rows = _rows(5, desized_per_day=10, full_per_day=10, desized_wins=9,
                 full_wins=5, session="london", factor=1.0)
    assert RP.session_multiplier_report(rows)["sessions"] == {}


def test_one_sided_data_is_untested():
    """No full-size cohort in the window means no comparison exists. The
    verdict has to say the test could not be run, not that it came back null."""
    rows = _rows(5, desized_per_day=10, full_per_day=0,
                 desized_wins=8, full_wins=0)
    g = RP.session_multiplier_report(rows)["sessions"]["newyork@0.5"]
    assert g["verdict"] == RP.SESSION_MULT_UNTESTED
    assert g["n_full"] == 0 and g["n_desized"] == 50


def test_below_the_floor_is_accumulate_and_offers_no_interval():
    """CLAUDE.md 2.4: no verdict below the sample floor. The gap is still
    returned - hiding it just gets it recomputed by hand next week."""
    rows = _rows(6, desized_per_day=4, full_per_day=4, desized_wins=4,
                 full_wins=0)          # 24 a side, below 30
    g = RP.session_multiplier_report(rows)["sessions"]["newyork@0.5"]
    assert g["verdict"] == RP.SESSION_MULT_ACCUMULATE
    assert min(g["n_desized"], g["n_full"]) == 24 < g["n_blocks"] * 30
    assert g["gap"] is not None          # reported, just not actionable
    assert "below the 30 floor" in g["reason"]


def test_a_single_day_block_is_undecidable_never_tight():
    """One block has zero between-block variance, so the interval collapses to a
    point. That is neither a null result nor a tight one, and it gets its own
    word so it can never be read as either."""
    rows = _rows(1, desized_per_day=40, full_per_day=40, desized_wins=36,
                 full_wins=20)
    g = RP.session_multiplier_report(rows)["sessions"]["newyork@0.5"]
    assert g["n_blocks"] == 1 and g["degenerate"] is True
    assert g["verdict"] == RP.SESSION_MULT_UNDECIDABLE
    assert "between-block variance" in g["reason"]


def test_the_inverted_case_the_issue_was_filed_about():
    """The de-sized cohort hits TP1 MORE often, interval excluding zero: we are
    cutting size on the better cohort. This is the shape the live newyork@0.5
    group returns (n 295 vs 998, 0.756 vs 0.667, gap +0.0886, 41 blocks)."""
    rows = _rows(20, desized_per_day=10, full_per_day=10, desized_wins=8,
                 full_wins=5)
    g = RP.session_multiplier_report(rows)["sessions"]["newyork@0.5"]
    assert g["verdict"] == RP.SESSION_MULT_INVERTED
    assert g["tp1_first_desized"] == 0.8 and g["tp1_first_full"] == 0.5
    assert g["gap"] == 0.3
    assert g["ci"][0] is not None and g["ci"][0] > 0      # excludes zero
    assert g["n_blocks"] == 20


def test_the_correct_case_reads_the_other_way():
    """A multiplier pointed the right way de-sizes a WORSE cohort. Both signs
    must be reachable or the report is an accusation, not an instrument."""
    rows = _rows(20, desized_per_day=10, full_per_day=10, desized_wins=3,
                 full_wins=8)
    g = RP.session_multiplier_report(rows)["sessions"]["newyork@0.5"]
    assert g["verdict"] == RP.SESSION_MULT_CORRECT
    assert g["gap"] < 0


def test_no_real_gap_is_no_evidence():
    rows = _rows(20, desized_per_day=10, full_per_day=10, desized_wins=6,
                 full_wins=6)
    g = RP.session_multiplier_report(rows)["sessions"]["newyork@0.5"]
    assert g["verdict"] == RP.SESSION_MULT_NO_EVIDENCE
    assert g["gap"] == 0.0


def test_the_hour_matched_row_is_always_present_and_names_its_hours():
    """The unrestricted comparison is confounded by hour of day, because a
    session IS a set of hours. A reader must not be able to miss the
    restriction, so it rides beside the headline rather than on request."""
    rows = _rows(20, desized_per_day=10, full_per_day=10, desized_wins=8,
                 full_wins=5)
    g = RP.session_multiplier_report(rows)["sessions"]["newyork@0.5"]
    m = g["hour_matched"]
    assert m["hours_utc"] == [12] and m["hour_min_n"] == 10
    assert m["verdict"] == RP.SESSION_MULT_INVERTED


def test_no_shared_hour_says_the_confound_could_not_be_removed():
    """Disjoint hours is the worst case for this comparison and the one most
    likely to be read as clean. It has to announce itself."""
    rows = _rows(20, desized_per_day=10, full_per_day=10, desized_wins=8,
                 full_wins=5, hour=12, full_hour=3)
    m = RP.session_multiplier_report(rows)["sessions"]["newyork@0.5"]["hour_matched"]
    assert m["hours_utc"] == []
    assert m["verdict"] == RP.SESSION_MULT_UNTESTED
    assert "cannot be removed" in m["reason"]


def test_the_report_is_deterministic():
    """A ruling that moves when you re-run it is not a ruling. Seeded, and the
    day keys are sorted before resampling."""
    rows = _rows(20, desized_per_day=10, full_per_day=10, desized_wins=8,
                 full_wins=5)
    a = RP.session_multiplier_report(rows)
    b = RP.session_multiplier_report(list(reversed(rows)))
    assert a == b


def test_a_row_with_no_day_cannot_join_a_block_and_is_dropped():
    """Never pooled into a fake block - the day IS the unit of resampling."""
    rows = _rows(20, desized_per_day=10, full_per_day=10, desized_wins=8,
                 full_wins=5)
    junk = [{"tp1_first": True, "day": None, "hour": 12,
             "session": "newyork", "factor": 0.5} for _ in range(500)]
    g = RP.session_multiplier_report(rows + junk)["sessions"]["newyork@0.5"]
    assert g["n_desized"] == 200


def test_several_sessions_and_factors_are_separate_groups():
    """Two multipliers are two experiments; pooling them describes neither."""
    rows = (_rows(20, desized_per_day=10, full_per_day=10, desized_wins=8,
                  full_wins=5)
            + _rows(20, desized_per_day=10, full_per_day=0, desized_wins=2,
                    full_wins=0, session="asian", factor=0.25))
    out = RP.session_multiplier_report(rows)
    assert set(out["sessions"]) == {"newyork@0.5", "asian@0.25"}
    assert out["sessions"]["asian@0.25"]["factor"] == 0.25
