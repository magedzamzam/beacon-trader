"""The research-only stop override (#245): it must move the stop, say so, and
never model an order the broker would refuse."""
from decimal import Decimal

import pytest

from harness import signal_overrides as SO


class _Sig:
    def __init__(self, entry, sl, direction="BUY"):
        self.entry_from, self.sl, self.direction = entry, sl, direction


# --- the spec ---------------------------------------------------------------

def test_no_block_means_no_override():
    assert SO.parse(None) is None
    assert SO.parse({}) is None


def test_a_spec_without_a_distance_is_refused():
    """Silently doing nothing would report the baseline as the treatment."""
    with pytest.raises(SO.OverrideError):
        SO.parse({"mode": "fixed_points"})


def test_a_nonpositive_or_unknown_spec_is_refused():
    with pytest.raises(SO.OverrideError):
        SO.parse({"sl_distance_points": 0})
    with pytest.raises(SO.OverrideError):
        SO.parse({"sl_distance_points": -5})
    with pytest.raises(SO.OverrideError):
        SO.parse({"sl_distance_points": 5, "mode": "wider_please"})


def test_an_empty_source_list_is_refused_not_read_as_all():
    """`sources: []` most likely means a config bug. Reading it as 'every
    source' would run the treatment over the whole book by accident."""
    with pytest.raises(SO.OverrideError):
        SO.parse({"sl_distance_points": 5, "sources": []})


# --- scoping ----------------------------------------------------------------

def test_omitting_sources_targets_every_source():
    spec = SO.parse({"sl_distance_points": 5})
    assert SO.applies_to(spec, 6) and SO.applies_to(spec, 99)


def test_a_source_list_targets_only_those():
    spec = SO.parse({"sl_distance_points": 5, "sources": [6, 4]})
    assert SO.applies_to(spec, 6) and not SO.applies_to(spec, 5)


def test_an_unattributed_signal_is_never_targeted():
    spec = SO.parse({"sl_distance_points": 5, "sources": [6]})
    assert not SO.applies_to(spec, None)


# --- the rewrite ------------------------------------------------------------

def test_a_buy_stop_goes_below_the_entry():
    spec = SO.parse({"sl_distance_points": 5})
    out, note = SO.apply(_Sig(Decimal("4300"), Decimal("4288")), 6, spec)
    assert note == "applied" and out.sl == Decimal("4295")


def test_a_sell_stop_goes_above_the_entry():
    spec = SO.parse({"sl_distance_points": 5})
    out, note = SO.apply(_Sig(Decimal("4300"), Decimal("4312"), "SELL"), 6, spec)
    assert note == "applied" and out.sl == Decimal("4305")


def test_the_original_signal_is_not_mutated():
    """The same ParsedSignal is replayed across accounts and variants. Mutating
    it would leak one variant's stop into the next."""
    sig = _Sig(Decimal("4300"), Decimal("4288"))
    out, _ = SO.apply(sig, 6, SO.parse({"sl_distance_points": 5}))
    assert sig.sl == Decimal("4288") and out is not sig


def test_an_untargeted_signal_comes_back_the_same_object():
    sig = _Sig(Decimal("4300"), Decimal("4288"))
    out, note = SO.apply(sig, 5, SO.parse({"sl_distance_points": 5, "sources": [6]}))
    assert out is sig and note is None


def test_cap_mode_leaves_an_already_tighter_stop_alone():
    spec = SO.parse({"sl_distance_points": 12, "mode": "cap_points"})
    out, note = SO.apply(_Sig(Decimal("4300"), Decimal("4297")), 6, spec)
    assert note == "already_tighter" and out.sl == Decimal("4297")


def test_fixed_mode_will_widen_if_that_is_what_was_asked():
    spec = SO.parse({"sl_distance_points": 12})
    out, note = SO.apply(_Sig(Decimal("4300"), Decimal("4297")), 6, spec)
    assert note == "applied" and out.sl == Decimal("4288")


# --- the thing that keeps the result honest ---------------------------------

def test_a_stop_inside_the_broker_minimum_is_refused_not_modelled():
    """An order the broker would reject is not a backtest result. The refusal
    is COUNTED so a variant that fell back on half the book is visible."""
    spec = SO.parse({"sl_distance_points": 2})
    out, note = SO.apply(_Sig(Decimal("4300"), Decimal("4288")), 6, spec,
                         min_stop_distance=Decimal("5"))
    assert note == "below_broker_minimum"
    assert out.sl == Decimal("4288")          # untouched — the channel's stop


def test_a_signal_with_no_geometry_is_skipped_and_counted():
    out, note = SO.apply(_Sig(None, None), 6, SO.parse({"sl_distance_points": 5}))
    assert note == "skipped_no_geometry"


def test_the_run_can_say_what_the_override_did():
    assert SO.summary(["applied", "applied", "below_broker_minimum", None]) == \
        {"applied": 2, "below_broker_minimum": 1}
