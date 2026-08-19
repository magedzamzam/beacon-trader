"""Which clock the signal-basis excursion window starts on (#232).

The window's start and the entry price it is measured against have to be the
same instant. For a channel signal they are: `created_at` is the only stamp
there is. For an engine signal (#224) they are not — the producer prices off a
bar CLOSE and writes the row a tick later — so the anchor has to prefer the
stamp the producer set.
"""
import datetime as dt

from beacon_core.analysis.excursion_store import signal_clock

UTC = dt.timezone.utc


class _Sig:
    def __init__(self, created_at, signal_at=None):
        self.created_at, self.signal_at = created_at, signal_at


def test_a_channel_signal_still_uses_its_write_time():
    """`signal_at` is NULL on every one of the 1,263 rows in the live table, so
    this change must be a no-op on all of them — otherwise it silently relabels
    the entire historical book."""
    wrote = dt.datetime(2026, 8, 12, 9, 30, tzinfo=UTC)
    assert signal_clock(_Sig(wrote)) == wrote


def test_an_engine_signal_is_scored_from_the_bar_it_fired_on():
    """The producer runs on a 300s loop against a 15m bar, so the write time
    trails the close by up to a tick. Scoring from the write time would drop
    that gap out of the window while keeping the close's price as the entry —
    an adverse move inside it would be invisible."""
    closed = dt.datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    wrote = closed + dt.timedelta(minutes=4, seconds=12)
    assert signal_clock(_Sig(wrote, signal_at=closed)) == closed


def test_the_gap_is_signed_the_same_way_every_time():
    """Why it is worth fixing for a four-minute error: the producer can only
    ever be LATE, so the bias never averages out across N."""
    closed = dt.datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    for lag_s in (1, 60, 299):
        s = _Sig(closed + dt.timedelta(seconds=lag_s), signal_at=closed)
        assert signal_clock(s) <= s.created_at


def test_a_row_without_the_column_at_all_does_not_explode():
    """`labels_by_signal` and the weekly hand this plain row-ish objects."""
    class _Bare:
        created_at = dt.datetime(2026, 8, 1, tzinfo=UTC)
    assert signal_clock(_Bare()) == _Bare.created_at
