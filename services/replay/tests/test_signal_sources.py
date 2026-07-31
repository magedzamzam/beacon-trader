"""The `signal_source` seam (§7).

The acceptance criterion is that adding a generator requires NO change to the
execution or metrics path. The only honest way to test that is to plug a fake
generator in and drive it through the same simulator the historical source uses,
asserting it produces a real trade — which is what this does.
"""
from __future__ import annotations

import datetime as dt

import pytest

from harness import signal_sources as SS
from harness.portfolio import PortfolioSim, SignalRow
from harness.variants import build_variant
from conftest import NO_RATCHET, T0, series, signal, variant_dict


def _fake_generator(bars, config):
    """A deliberately trivial generator: one BUY on the Nth bar. It exists to
    prove the seam, not to be a strategy — §8 is explicit that a generator is a
    curve-fitting machine and none ships with Phase 1."""
    idx = int(config.get("bar_index", 1))
    if len(bars) <= idx:
        return []
    return [SS.GeneratedSignal(bars[idx].ts, signal())]


@pytest.fixture(autouse=True)
def _clean_registry():
    SS._GENERATORS.clear()
    yield
    SS._GENERATORS.clear()


def test_the_historical_source_is_the_default_and_is_not_a_generator():
    assert SS.is_generator(SS.HISTORICAL) is False
    assert SS.is_generator("generator:fvg") is True
    assert SS.generator_name("generator:fvg") == "fvg"


def test_no_generator_ships_by_default():
    """Phase 1 delivers the seam, not the curve-fitting machine."""
    assert SS.available_generators() == []


def test_an_unknown_generator_names_what_is_registered():
    with pytest.raises(KeyError) as exc:
        SS.resolve_generator("generator:nope")
    assert "nope" in str(exc.value)


def test_a_registered_generator_emits_the_existing_parsed_signal_shape():
    SS.register_generator("fake", _fake_generator)
    s = series([4020] * 5)
    out = SS.run_generator("generator:fake", s.bars, {"bar_index": 1})
    assert len(out) == 1
    at, parsed = out[0]
    assert at == s[1].ts
    assert parsed.symbol == "XAUUSD" and parsed.direction == "BUY"
    assert parsed.tps and parsed.sl


def test_generated_signals_are_sorted_by_time():
    def _unsorted(bars, config):
        return [SS.GeneratedSignal(bars[3].ts, signal()),
                SS.GeneratedSignal(bars[1].ts, signal())]
    SS.register_generator("unsorted", _unsorted)
    out = SS.run_generator("generator:unsorted", series([4020] * 6).bars, {})
    assert [g.at for g in out] == sorted(g.at for g in out)


def test_a_generated_signal_reaches_the_same_simulator_unchanged():
    """The whole point of the seam: no parallel execution stack. A generated
    signal is planned, sized, filtered, capped and scored by exactly the code
    a Telegram signal is."""
    SS.register_generator("fake", _fake_generator)
    s = series([4020, 4020, (4006, 4006, 3999, 4005),
                (4005, 4032, 4004, 4031)] + [4031] * 6)
    generated = SS.run_generator("generator:fake", s.bars, {"bar_index": 1})
    rows = [SignalRow(id=1000 + i, at=g.at, parsed=g.parsed, source_id=None,
                      account_ids=(1,)) for i, g in enumerate(generated)]
    v = build_variant(variant_dict(sl_rules=NO_RATCHET))
    res = PortfolioSim(v, s).run(rows)
    assert res.counts["taken"] == 1
    assert res.trades[0].ever_filled is True
    assert res.trades[0].realized_pl != 0


def test_a_generator_row_without_a_timestamp_is_dropped_not_guessed():
    """A signal with no instant cannot be replayed — there is no bar to open the
    window at, and inventing one is look-ahead."""
    def _bad(bars, config):
        return [SS.GeneratedSignal(None, signal()),
                SS.GeneratedSignal(bars[1].ts, signal())]
    SS.register_generator("bad", _bad)
    out = SS.run_generator("generator:bad", series([4020] * 4).bars, {})
    assert len(out) == 1
