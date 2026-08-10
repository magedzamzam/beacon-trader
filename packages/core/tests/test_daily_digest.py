"""The daily operator digest and the broker-truth arithmetic under it (#198).

`daily_summary` was routed, given an emoji, and fired by nothing — an operator
could route it to a channel and write a template that never sent. These tests
pin the two things that make it real: a schedule that sends exactly one digest
per day across restarts, and a P&L figure that is the account's, not the
ledger's.
"""
import datetime as dt
from pathlib import Path

from beacon_core.analysis import broker_truth as BT
from beacon_core.notifications import digest as DG
from beacon_core.notifications import templates as NT

REPO_ROOT = Path(__file__).resolve().parents[3]
UTC = dt.timezone.utc


def _at(y, m, d, hh=0, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=UTC)


# --- the contract is no longer a lie ------------------------------------------
def test_daily_summary_is_emitted_now():
    """`is_emitted` was False because the contract was empty, which is why the
    editor greyed it out. The contract is the promise; the emitter below keeps it."""
    assert NT.is_emitted("daily_summary")
    assert set(NT.EVENT_FIELDS["daily_summary"]) >= {
        "date", "pl", "wins", "losses", "open_positions", "drawdown"}


def test_every_digest_token_exists_in_the_field_registry():
    """The editor's field picker is derived from FIELDS, so a contract token
    with no registry entry would advertise a chip the renderer cannot fill."""
    known = {name for name, _l, _e in NT.FIELDS}
    assert set(NT.EVENT_FIELDS["daily_summary"]) <= known


def test_something_actually_fires_it():
    monitor = (REPO_ROOT / "services/monitor/main.py").read_text(encoding="utf-8")
    assert '_notify("daily_summary"' in monitor
    assert "await _maybe_send_daily_digest()" in monitor


def test_both_write_paths_apply_the_same_sign_rule():
    """#202's root cause was one path repairing the sign and the other not.
    They now call the same function, and this fails if either stops."""
    monitor = (REPO_ROOT / "services/monitor/main.py").read_text(encoding="utf-8")
    close_leg = monitor.split("realized_pl = m.get(\"pl\")", 1)[1].split(
        "async def _audit_activities", 1)[0]
    audit = monitor.split("async def _audit_activities", 1)[1].split(
        "\n        # --- pass 1", 1)[0]
    assert "BT.signed_close_pl(" in close_leg, "the leg path lost its sign repair"
    assert "BT.signed_close_pl(" in audit, "the audit path is writing raw broker P&L"
    # And the old inline rule must not creep back as a second copy.
    assert "realized_pl = -realized_pl" not in monitor


# --- the schedule --------------------------------------------------------------
def test_it_reports_the_last_COMPLETE_day():
    """A digest of a day still in progress is a number that changes after you
    have read it."""
    assert DG.digest_due(None, _at(2026, 8, 10, 0, 30)) == "2026-08-09"


def test_nothing_is_sent_before_the_send_time():
    assert DG.digest_due(None, _at(2026, 8, 10, 0, 10), "00:15") is None
    assert DG.digest_due(None, _at(2026, 8, 10, 0, 15), "00:15") == "2026-08-09"


def test_exactly_one_per_day_across_a_restart():
    """The guard stores the DATE covered, not the send time, so a monitor that
    restarts three times after the send window does not send three digests."""
    now = _at(2026, 8, 10, 6, 0)
    assert DG.digest_due("2026-08-09", now) is None
    assert DG.digest_due("2026-08-08", now) == "2026-08-09"


def test_a_monitor_that_was_down_sends_once_not_a_backlog():
    """Yesterday's digest at 15:00 is useful. Six stale ones in a row is how a
    channel gets muted."""
    assert DG.digest_due("2026-08-03", _at(2026, 8, 10, 15, 0)) == "2026-08-09"


def test_a_broken_send_time_falls_back_instead_of_stopping_the_digest():
    assert DG.digest_due(None, _at(2026, 8, 10, 12, 0), "not-a-time") == "2026-08-09"


# --- the money ----------------------------------------------------------------
def _act(deal, at, typ="POSITION", pl=0.0, trade=1, acct=5, **kw):
    return {"account_id": acct, "deal_id": deal, "activity_at": at,
            "type": typ, "trade_id": trade, "realized_pl": pl, **kw}


def test_partial_closes_all_count():
    """The tempting `MAX(realized_pl) GROUP BY deal_id` drops every partial
    close but the largest — on a laddered book that is most of the money. The
    identity is the four columns the table's own unique constraint declares."""
    rows = [_act("d1", _at(2026, 8, 9, 10), pl=100.0),
            _act("d1", _at(2026, 8, 9, 11), pl=50.0),
            _act("d1", _at(2026, 8, 9, 12), pl=25.0)]
    assert BT.realized_by_trade(rows) == {1: 175.0}


def test_the_duplicate_record_pair_is_collapsed_once():
    """Same deal, same instant, same type, two feeds differing only by `source`.
    Counting both would double the day."""
    at = _at(2026, 8, 9, 10)
    rows = [_act("d1", at, pl=100.0, source="USER"),
            _act("d1", at, pl=100.0, source="TP")]
    assert BT.realized_by_trade(rows) == {1: 100.0}


def test_rows_with_no_money_are_not_zeroes():
    """An EDIT_STOP activity is not a close. Treating it as a 0.0 close would
    drag the win/loss counts toward a flat day that never happened."""
    rows = [_act("d1", _at(2026, 8, 9, 9), typ="EDIT_STOP_AND_LIMIT", pl=None),
            _act("d1", _at(2026, 8, 9, 10), pl=100.0)]
    assert BT.realized_by_trade(rows) == {1: 100.0}


def test_dedupe_does_not_depend_on_row_order():
    at = _at(2026, 8, 9, 10)
    a = [_act("d1", at, pl=100.0), _act("d1", at, pl=100.0)]
    assert BT.realized_by_trade(a) == BT.realized_by_trade(list(reversed(a)))


def test_drawdown_is_peak_to_trough_not_the_net():
    """A day that made 500, gave back 800, then recovered to +200 was a −800 day
    to live through. That is the number an operator means, and it is not
    recoverable from the net."""
    assert BT.max_drawdown([500.0, -800.0, 500.0]) == -800.0
    assert BT.max_drawdown([100.0, 200.0]) == 0.0          # never below its peak
    assert BT.max_drawdown([]) == 0.0
    assert BT.max_drawdown([-50.0]) == -50.0               # peak is the 0 it started at


# --- the phantom tax, and which way it actually pointed (#202) ----------------
def test_a_ratcheted_stop_out_is_a_loss_however_the_broker_sent_it():
    """The whole of #202 in one line. Capital.com returns the transaction amount
    UNSIGNED when the stop was modified before the close, so a ratcheted
    stop-out arrives as +89.71. `_close_leg` always repaired this; the activity
    audit did not, and the two ledgers drifted apart by exactly twice the
    ratcheted losses."""
    assert BT.signed_close_pl(89.71, "sl_hit") == -89.71
    assert BT.signed_close_pl(-89.71, "sl_hit") == -89.71     # already signed


def test_a_winning_close_is_never_flipped():
    assert BT.signed_close_pl(284.34, "tp_hit") == 284.34


def test_a_breakeven_is_left_exactly_as_the_broker_sent_it():
    """A breakeven can legitimately land either side of zero, and those rows
    already agree (657 of 657 exact on the live book). Correcting them would
    invent an error rather than fix one."""
    assert BT.signed_close_pl(12.5, "breakeven") == 12.5
    assert BT.signed_close_pl(-12.5, "breakeven") == -12.5


def test_a_close_with_no_money_stays_none():
    assert BT.signed_close_pl(None, "sl_hit") is None


def test_the_breaker_records_the_basis_it_fired_on():
    """A halt is only reviewable if the number it fired on says where it came
    from. And the basis must stay `trades.realized_pl`: the note that used to
    stand there told the next reader to feed broker truth instead, which — now
    that the activity ledger is known to under-report losses — would blind the
    one guard whose whole job is to see them."""
    ex = (REPO_ROOT / "services/executor/main.py").read_text(encoding="utf-8")
    assert ex.count('"pl_basis": "trades.realized_pl"') == 2   # hard + soft
    assert "feed broker-truth realized before enabling" not in ex


def test_the_gap_is_reported_per_account_not_reconciled_away():
    """The two ledgers are built from different records, so a divergence means
    one of them has stopped tracking the account. That is a thing to be told
    about, not silently averaged."""
    out = BT.phantom_gap({1: 100.0, 2: -50.0}, {1: 100.0, 2: 50.0})
    assert out["gap"] == -100.0 and out["material"]
    assert out["worst_trade"] == 2 and out["worst_gap"] == -100.0


def test_agreeing_ledgers_are_not_flagged():
    out = BT.phantom_gap({1: 100.0}, {1: 100.4})
    assert not out["material"] and out["gap"] == -0.4


def test_the_gap_counts_a_trade_missing_from_either_side():
    """A trade the broker recorded and the ledger did not is the same defect
    class, so it must not fall out of the comparison."""
    out = BT.phantom_gap({1: 100.0, 2: 20.0}, {1: 100.0})
    assert out["n_trades"] == 2 and out["gap"] == 20.0


def test_settled_pl_says_which_basis_each_trade_came_from():
    """A mixed number presented as if it had one basis is how a 12k/week gap
    hides. The counts are returned so the caller can state it."""
    rows = [_act("d1", _at(2026, 8, 9, 10), pl=100.0, trade=1)]
    out = BT.settled_pl(rows, ledger={1: -999.0, 2: 40.0})
    assert out["by_trade"] == {1: 100.0, 2: 40.0}     # broker wins where it spoke
    assert out["n_broker"] == 1 and out["n_ledger"] == 1
