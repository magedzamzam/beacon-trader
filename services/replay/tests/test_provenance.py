"""Imported history must not be priced as the account's past (#192).

`signals.created_at` is INGEST time. A channel's backlog arrives in one burst, so
230 of 856 rows on the current book share a 15-minute window and NONE of the 179
in the biggest one ever produced a trade. Replaying them puts money on
opportunities that did not exist.

The acceptance criterion is stated as a property of the SET a P&L report draws:
no window of 10+ signals sharing a 15m bar, unless the caller opted in. That is
what these tests assert — once at the pure layer (`provenance`), once at the
loader's SQL (`load_signals` excludes by default), and once over the call sites
(only the coverage diagnostic opts in).
"""
from __future__ import annotations

import ast
import asyncio
import datetime as dt
import inspect
from pathlib import Path

from harness import provenance as P
from harness import store
from harness import whatif as W
from harness.portfolio import SignalRow
from conftest import T0, signal

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def _row(sid: int, at, backfilled: bool) -> SignalRow:
    return SignalRow(id=sid, at=at, parsed=signal(), source_id=7,
                     source_name="src", account_ids=(1,), backfilled=backfilled)


def _burst(n: int, *, at=T0, backfilled: bool, first_id: int = 1):
    """`n` signals all stamped the same moment — an imported backlog."""
    return [_row(first_id + i, at, backfilled) for i in range(n)]


def _spread(n: int, *, start=T0 + dt.timedelta(days=1), minutes: int = 60,
            first_id: int = 1000):
    """`n` signals an hour apart, starting a day clear of the burst — a normal
    book, and no accidental overlap with the imported block."""
    return [_row(first_id + i, start + dt.timedelta(minutes=minutes * i), False)
            for i in range(n)]


# --- the detector -------------------------------------------------------------

def test_a_bulk_import_is_one_window_not_a_busy_hour():
    blocks = P.bursts(_burst(12, backfilled=True))
    assert list(blocks.values()) == [12]
    assert list(blocks)[0] == P.bar_key(T0)


def test_signals_spread_across_the_book_are_not_a_burst():
    assert P.bursts(_spread(40)) == {}


def test_a_window_under_the_threshold_is_not_flagged():
    # 9 is the busiest 15 minutes a real trade book has shown; the line sits above it.
    assert P.bursts(_burst(P.BULK_MIN_SIGNALS - 1, backfilled=False)) == {}
    assert P.bursts(_burst(P.BULK_MIN_SIGNALS, backfilled=False))


def test_a_row_with_an_unusable_timestamp_is_skipped_not_raised_on():
    class _Bad:
        at = None
    assert P.bursts([_Bad()] + _burst(11, backfilled=True))[P.bar_key(T0)] == 11


# --- the acceptance criterion, as stated --------------------------------------

def test_a_pnl_signal_set_has_no_bulk_import_window():
    """THE criterion: what a P&L report draws contains no 15m window holding 10+
    signals — because the imported block is excluded, not because it was small."""
    mixed = _burst(179, backfilled=True) + _spread(20)
    drawn = P.pnl_set(mixed)
    assert len(drawn) == 20
    assert P.bursts(drawn) == {}


def test_a_caller_that_opts_in_gets_the_whole_set_burst_and_all():
    mixed = _burst(179, backfilled=True) + _spread(20)
    drawn = P.pnl_set(mixed, include_backfilled=True)
    assert len(drawn) == 199
    assert P.bursts(drawn) == {P.bar_key(T0): 179}


def test_a_signal_with_no_provenance_reads_as_live():
    """Pre-migration rows carry no flag at all. The default direction has to be
    KEEP: dropping a signal for lack of provenance would silently shrink the
    book, which is the same class of error in the other direction."""
    class _Old:
        at = T0
    old = _Old()
    assert P.is_backfilled(old) is False
    assert P.live_only([old]) == [old]
    assert P.pnl_set([old]) == [old]


# --- the loader ---------------------------------------------------------------

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    """Captures the statements `load_signals` builds. No DB — the assertion is
    about the WHERE clause, and a live Postgres would prove less, not more."""

    def __init__(self):
        self.statements = []

    async def execute(self, q):
        self.statements.append(q)
        return _Result([])


def _signals_clause(**kw) -> str:
    s = _Session()
    asyncio.run(store.load_signals(s, **kw))
    return str(s.statements[0])


def test_load_signals_excludes_backfilled_by_default():
    assert (inspect.signature(store.load_signals)
            .parameters["include_backfilled"].default is False)
    assert "backfilled IS NOT true" in _signals_clause()


def test_load_signals_keeps_backfilled_only_when_asked():
    # The column still appears in the SELECT list — it is mapped. What must be
    # gone is the WHERE that filters on it.
    assert "backfilled IS NOT" not in _signals_clause(include_backfilled=True)


def test_the_exclusion_is_is_not_true_so_null_rows_survive():
    """`backfilled = false` would drop every row written before the migration
    added the column — the whole book, on the box that matters."""
    clause = _signals_clause()
    assert "backfilled = false" not in clause.lower()


# --- the call sites -----------------------------------------------------------

def _opt_in_callers() -> set[str]:
    """Functions in `main.py` that pass `include_backfilled=True`."""
    tree = ast.parse((SERVICE_ROOT / "main.py").read_text(encoding="utf-8"))
    out = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call):
                continue
            if any(k.arg == "include_backfilled"
                   and isinstance(k.value, ast.Constant) and k.value.value is True
                   for k in call.keywords):
                out.add(fn.name)
    return out


def test_only_the_coverage_diagnostic_opts_into_imported_history():
    """A new opt-in has to be a deliberate edit to this test, not a keyword
    someone copied into a P&L path."""
    assert _opt_in_callers() == {"cmd_coverage"}


# --- the disclosure half stays ------------------------------------------------

def test_an_unmarked_burst_is_still_declared_in_words():
    """The flag is authoritative but retrospective. The next onboarding lands
    unmarked, and the what-if page still has to say so."""
    rows = _burst(30, backfilled=False) + _spread(10)
    caveats = W.bulk_ingest_caveats(rows, {})
    assert caveats and "burst" in caveats[0]
    assert "30" in caveats[0]


def test_whatif_and_the_loader_agree_on_what_a_burst_is():
    assert (W.BULK_BAR_MINUTES, W.BULK_MIN_SIGNALS) == (P.BULK_BAR_MINUTES,
                                                        P.BULK_MIN_SIGNALS)
