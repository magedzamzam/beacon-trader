"""The `signal_source` seam (#169 §7).

    historical        -> read `signals` rows                  (Phase 1, what-if)
    generator:<name>  -> scan candles, emit signals           (Phase 2, backtest)

Both produce `SignalRow`s carrying the EXISTING `ParsedSignal` shape, so
everything downstream — planner, sizing, sl_rules, staging, risk caps, metrics —
is byte-identical to the Telegram path. There is no parallel execution stack,
and adding a generator requires no change to the execution or metrics code. The
test suite proves that by plugging a fake generator in and asserting it reaches
the same simulator.

NO GENERATOR SHIPS WITH PHASE 1, deliberately. The seam is the deliverable; the
generators are the part §8 calls a curve-fitting machine, and building one
before the harness has passed its own validation gate would be fitting a model
to a simulator nobody has checked yet.

PATH TO LIVE, unchanged: a validated generator does NOT go live from a backtest.
It needs a `kind='engine'` source (which does not exist today — `sources.kind` is
telegram | tradingview | manual | api), a producer, and shadow forward-R, and
only then a weekend config act on one arm. Backtest is the SCREENING step.
"""
from __future__ import annotations

import datetime as dt
from typing import Callable, Dict, List, NamedTuple, Sequence

from beacon_core.parsing.models import ParsedSignal

from . import bars as B

HISTORICAL = "historical"
GENERATOR_PREFIX = "generator:"


class GeneratedSignal(NamedTuple):
    """A generator's output. `parsed` is the shipped `ParsedSignal` — symbol,
    direction, entry_from/entry_to, sl, tps — exactly as the Telegram parser
    emits, so nothing downstream can tell the difference.

    `at` is the only addition, and it is unavoidable: a signal with no instant
    cannot be replayed, because there is no bar to open the window at. A
    generator must therefore emit the CLOSE time of the bar that triggered it,
    never the bar it is reacting to — emitting the trigger bar's open time is
    look-ahead by one bar and would flatter every result."""
    at: dt.datetime
    parsed: ParsedSignal


# `(bars, config) -> [GeneratedSignal]`. Pure by contract: no DB, no clock, no
# broker. Each generator defines its own SL and TP ladder — that is what makes it
# a signal, and what R is measured against.
Generator = Callable[[Sequence[B.Bar], dict], List[GeneratedSignal]]

_GENERATORS: Dict[str, Generator] = {}


def register_generator(name: str, fn: Generator) -> None:
    _GENERATORS[name] = fn


def available_generators() -> List[str]:
    return sorted(_GENERATORS)


def is_generator(spec: str) -> bool:
    return str(spec or "").startswith(GENERATOR_PREFIX)


def generator_name(spec: str) -> str:
    return str(spec)[len(GENERATOR_PREFIX):]


def resolve_generator(spec: str) -> Generator:
    name = generator_name(spec)
    fn = _GENERATORS.get(name)
    if fn is None:
        raise KeyError(f"unknown generator {name!r}; registered: "
                       f"{', '.join(available_generators()) or '(none)'}")
    return fn


def run_generator(spec: str, bars: Sequence[B.Bar], config: dict) -> List[GeneratedSignal]:
    """Invoke a registered generator and sanity-check its output.

    A generator that emits an unusable geometry is a bug in the generator, not a
    signal the harness should quietly plan around — but it must not kill the run
    either, so bad rows are dropped and the caller sees the count."""
    out = []
    for item in (resolve_generator(spec)(bars, config or {}) or []):
        at, parsed = item
        if at is None or parsed is None:
            continue
        out.append(GeneratedSignal(at, parsed))
    out.sort(key=lambda g: g.at)
    return out
