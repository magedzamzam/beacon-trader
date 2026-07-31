"""Burst debounce for broker_error (#180). Pure — no DB/crypto/network."""
from beacon_core.notifications.throttle import Throttle, suffix


def test_first_call_sends_and_the_rest_of_the_window_does_not():
    t = Throttle(window_sec=60)
    assert t.allow("k", now=0.0) == (True, 0)
    assert t.allow("k", now=1.0) == (False, 0)
    assert t.allow("k", now=59.9) == (False, 0)


def test_next_window_sends_and_reports_the_burst_it_swallowed():
    t = Throttle(window_sec=60)
    t.allow("k", now=0.0)
    for i in range(7):
        t.allow("k", now=1.0 + i)
    # the alert after the window says how many rejections it stood for
    assert t.allow("k", now=61.0) == (True, 7)
    # and the count resets — the next one doesn't re-report the old burst
    assert t.allow("k", now=200.0) == (True, 0)


def test_keys_are_independent_so_one_storm_never_masks_another():
    t = Throttle(window_sec=60)
    assert t.allow("reject:1:XAUUSD", now=0.0)[0] is True
    # a DIFFERENT failure in the same window must still get through
    assert t.allow("sl_move:1:XAUUSD", now=0.1)[0] is True
    assert t.allow("reject:2:XAUUSD", now=0.2)[0] is True
    assert t.allow("reject:1:XAUUSD", now=0.3)[0] is False


def test_elapsed_keys_are_pruned_but_pending_burst_counts_survive():
    t = Throttle(window_sec=10)
    for i in range(600):                    # push past _MAX_KEYS
        t.allow(f"k{i}", now=float(i))
    t.allow("hot", now=600.0)
    t.allow("hot", now=601.0)               # one suppressed, not yet reported
    for i in range(600, 1200):
        t.allow(f"k{i}", now=float(i))
    assert len(t._last) < 1200               # pruning happened
    assert t.allow("hot", now=2000.0) == (True, 1)   # the count was not lost


def test_suffix_only_annotates_a_real_burst():
    assert suffix("Order rejected: market closed", 0) == "Order rejected: market closed"
    assert "+4 more" in suffix("Order rejected: market closed", 4)
