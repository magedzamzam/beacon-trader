"""cancel_pending_on_stop: retire a trade's resting orders when it dies (#161).

An armed reclaim STOP used to rest live at the broker for its whole TTL (~60 min)
after the trade had already been stopped out, because the cancel gate required a
TP hit or an SL ratchet — a straight-to-SL trade has neither. `cancel_reason` is
the decision, split out pure so the gate is testable without a broker.
"""
from beacon_core.execution import staging as STG
from beacon_core.execution import strategy as ST


# --- the gate --------------------------------------------------------------------
def test_straight_to_sl_now_cancels():
    # THE bug: t391 — toe-in filled MARKET, reclaim STOP armed, then straight to SL.
    # No TP hit, no ratchet, so `progressed` never fired and the STOP rested until
    # its TTL. The trade has no open position left: everything resting is orphaned.
    assert ST.cancel_reason(["closed", "working"]) == ST.CANCEL_STOPPED_OUT


def test_progress_still_cancels_the_stale_ladder():
    # Regression on the pre-#161 behaviour (#25): a 4025-4020 buy where 4025 filled
    # and hit TP1 while 4020 never triggered.
    assert ST.cancel_reason(["open", "working"], tps_hit=True) == ST.CANCEL_PROGRESSED
    assert ST.cancel_reason(["open", "working"], sl_moved=True) == ST.CANCEL_PROGRESSED


def test_open_and_unmoved_leaves_the_ladder_working():
    # The trade is alive and has not gone our way — the lower rung is a legitimate
    # resting entry. Nothing to retire.
    assert ST.cancel_reason(["open", "working"]) is None


def test_never_tears_down_a_ladder_we_were_never_in():
    # #25: a phantom TP computed off price with ZERO fills must not cancel entries.
    assert ST.cancel_reason(["working", "working"], tps_hit=True) is None
    assert ST.cancel_reason(["staged", "working"], tps_hit=True, sl_moved=True) is None
    assert ST.cancel_reason([]) is None
    # ...but an expired/cancelled sibling is not a fill either.
    assert ST.cancel_reason(["expired", "working"], tps_hit=True) is None


def test_stopped_out_wins_over_progressed():
    # A trade that hit TP1 and then stopped the runner out is BOTH; `stopped_out` is
    # the stronger claim (the trade is over, not merely stale) and must be reported
    # so the caller also retires the never-placed staged tranches.
    assert ST.cancel_reason(["closed", "closed", "working"],
                            tps_hit=True, sl_moved=True) == ST.CANCEL_STOPPED_OUT


def test_a_fill_that_closed_on_an_earlier_tick_still_counts():
    # `cancel_reason` is fed EVERY leg of the trade, not just the open ones — a
    # position that filled and closed ticks ago is still a fill.
    assert ST.cancel_reason(["closed"]) == ST.CANCEL_STOPPED_OUT
    assert ST.cancel_reason(["closed", "expired", "cancelled"]) == ST.CANCEL_STOPPED_OUT


def test_partially_closed_trade_is_not_stopped_out():
    # TP1's leg closed, the runner is still open: not terminal, so a resting rung
    # only goes if the trade actually progressed.
    assert ST.cancel_reason(["closed", "open", "working"]) is None
    assert ST.cancel_reason(["closed", "open", "working"],
                            tps_hit=True) == ST.CANCEL_PROGRESSED


# --- tranche bookkeeping ---------------------------------------------------------
def test_armed_is_not_a_terminal_tranche_state():
    # The second half of #161: 15 CLOSED trades still read state='armed' with a live
    # broker_order_ref, so "is an order still resting?" was unanswerable from the DB.
    # `armed`/`deployed` must stay resolvable; everything else is done.
    assert STG.ARMED not in STG.TERMINAL_STATES
    assert STG.DEPLOYED not in STG.TERMINAL_STATES
    assert STG.PENDING not in STG.TERMINAL_STATES
    for state in (STG.FILLED, STG.EXPIRED, STG.SKIPPED, STG.CANCELLED):
        assert state in STG.TERMINAL_STATES


def test_cancelled_state_fits_the_column():
    # staged_tranches.state is String(12).
    for state in STG.TERMINAL_STATES + (STG.PENDING, STG.DEPLOYED, STG.ARMED):
        assert len(state) <= 12
