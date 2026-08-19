"""Engine signals reach the ledger and never reach the broker (#224, step 4)."""
import datetime as dt

import pytest

from beacon_core.generator import producer as P
from beacon_core.generator import rules as G

RUN43 = {
    "timeframe": "15m",
    "long": {"when": {"type": "indicator", "id": "rsi", "timeframe": "15m",
                      "field": "value", "op": "lt", "value": 70}},
    "entry": {"type": "close"},
    "sl": {"type": "atr_mult", "timeframe": "1h", "period": 14, "mult": 1.5},
    "tps": [{"type": "r_mult", "r": 1.0}, {"type": "r_mult", "r": 2.0}],
    "cooldown_bars": 4, "max_signals_per_day": 2,
}
UTC = dt.timezone.utc


class _Bar:
    def __init__(self, ts, close=100.0):
        self.ts, self.close = ts, close


class _Provider:
    def atr(self, timeframe, when, period):
        return 10.0


def _frame(n=40, start=dt.datetime(2026, 8, 18, 0, 0, tzinfo=UTC)):
    return [_Bar(start + dt.timedelta(minutes=15 * i)) for i in range(n)]


def _ctx(rsi=50):
    return {"price": 100.0, "ta": {"15m": {"rsi": {"value": rsi}}}}


# --- the safety property ----------------------------------------------------

def test_the_producer_cannot_reach_the_executor():
    """A signal reaches the executor exactly once, by being enqueued onto the
    durable queue. This module must have no route to it -- structural, not a
    flag that could be flipped.

    Asserted over the AST rather than the text: the module DISCUSSES the queue
    in its docstring, and a grep would either fail on the prose or be weakened
    until it passed. Imports and calls are the things that can actually reach
    it."""
    import ast
    tree = ast.parse(open(P.__file__.replace(".pyc", ".py"), encoding="utf-8").read())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update("%s.%s" % (node.module or "", a.name) for a in node.names)
    assert not [m for m in imported if "bus" in m or "queue" in m], imported

    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            called.add(f.attr if isinstance(f, ast.Attribute)
                       else getattr(f, "id", ""))
    assert "enqueue" not in called and "publish" not in called, called


def test_a_shadow_row_is_not_an_executed_one():
    parsed, _ = G.build_signal(G.RulesSpec(RUN43), "BUY", 100.0, _Provider(),
                               dt.datetime(2026, 8, 18, 9, tzinfo=UTC), _ctx())
    row = P.shadow_signal_row(parsed, source_id=42, closed_at=parsed and
                              dt.datetime(2026, 8, 18, 9, tzinfo=UTC))
    assert row["status"] == "shadow"
    assert row["source_id"] == 42
    assert row["signal_at"] == dt.datetime(2026, 8, 18, 9, tzinfo=UTC)


# --- the closed-bar boundary ------------------------------------------------

def test_a_bar_still_open_is_not_read():
    """Reading it would ask the condition about a high that has not printed, and
    the backtest -- which only ever sees complete buckets -- never would."""
    frame = _frame()
    last = frame[-1]
    idx, bar, closed = P.latest_closed_bar(
        frame, last.ts + dt.timedelta(minutes=5), 15)
    assert bar is not frame[-1]           # the newest bucket is still forming
    assert closed == last.ts             # so the previous one is the newest closed


def test_the_bar_is_read_once_it_has_closed():
    frame = _frame()
    last = frame[-1]
    idx, bar, closed = P.latest_closed_bar(
        frame, last.ts + dt.timedelta(minutes=15), 15)
    assert bar is last and closed == last.ts + dt.timedelta(minutes=15)


def test_warmup_is_respected():
    out = P.evaluate_latest(G.RulesSpec(RUN43), _Provider(), _frame(5),
                            _ctx(), dt.datetime(2026, 8, 19, tzinfo=UTC))
    assert out["signal"] is None and out["reason"] == "warmup"


# --- caps derived from the ledger, so a restart cannot reset them ------------

def test_cooldown_is_measured_against_the_last_signal_in_the_ledger():
    """A CapState held in memory dies with the process, and the cooldown that
    exists to stop a persistent condition emitting every bar would reset with
    it."""
    spec = G.RulesSpec(RUN43)          # 4 bars x 15m = 60 minutes
    closed = dt.datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    recent = closed - dt.timedelta(minutes=30)
    assert P.suppressed_by_ledger(spec, closed, recent, 0) == "n_suppressed_cooldown"
    old = closed - dt.timedelta(minutes=90)
    assert P.suppressed_by_ledger(spec, closed, old, 0) is None


def test_the_daily_cap_counts_what_is_already_written():
    spec = G.RulesSpec(RUN43)          # max 2/day
    closed = dt.datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    assert P.suppressed_by_ledger(spec, closed, None, 2) == "n_suppressed_max_per_day"
    assert P.suppressed_by_ledger(spec, closed, None, 1) is None


# --- the decision, end to end -----------------------------------------------

def test_a_triggering_bar_produces_a_priced_signal():
    frame = _frame()
    now = frame[-1].ts + dt.timedelta(minutes=15)
    out = P.evaluate_latest(G.RulesSpec(RUN43), _Provider(), frame, _ctx(50), now)
    assert out["reason"] is None and out["direction"] == "BUY"
    assert float(out["signal"].sl) == pytest.approx(85.0)
    assert out["closed_at"] == now


def test_every_non_emission_says_which_kind_it_was():
    """`nothing emitted` has several very different causes and a producer that
    cannot tell them apart is one nobody can debug."""
    frame = _frame()
    now = frame[-1].ts + dt.timedelta(minutes=15)
    spec = G.RulesSpec(RUN43)
    # condition false
    assert P.evaluate_latest(spec, _Provider(), frame, _ctx(90), now)["reason"] \
        == "no_trigger"
    # condition unknown (no ta block at all)
    assert P.evaluate_latest(spec, _Provider(), frame, {"price": 100.0},
                             now)["reason"] == "n_unknown"
    # capped
    assert P.evaluate_latest(spec, _Provider(), frame, _ctx(50), now,
                             count_today=99)["reason"] == "n_suppressed_max_per_day"

    class _NoAtr:
        def atr(self, *a, **k):
            return None
    assert P.evaluate_latest(spec, _NoAtr(), frame, _ctx(50), now)["reason"] \
        .startswith("dropped_geometry:")


def test_the_signal_is_stamped_at_the_bar_close_not_the_write_time():
    """Forward R is measured from the moment the condition became true; a
    producer that ran late would otherwise score itself from whenever it woke."""
    frame = _frame()
    closed = frame[-1].ts + dt.timedelta(minutes=15)
    late = closed + dt.timedelta(minutes=7)
    out = P.evaluate_latest(G.RulesSpec(RUN43), _Provider(), frame, _ctx(50), late)
    assert out["closed_at"] == closed

# --- catching up bars the producer was not awake for (#239) -----------------

def test_only_closed_bars_are_offered():
    """A bucket still forming is not offered for catch-up either — the whole
    point of reading closed bars is that the high has printed."""
    frame = _frame(10)
    last = frame[-1]
    got = P.closed_bars(frame, last.ts + dt.timedelta(minutes=5), 15)
    assert all(closed <= last.ts + dt.timedelta(minutes=5) for _, _, closed in got)
    assert got[-1][1] is frame[-2]          # newest closed, not the forming one


def test_bars_come_back_oldest_first():
    """Order is the whole correctness argument: caps are applied as the bars
    actually happened, so a catch-up cannot emit a later bar and then let an
    earlier one through the cooldown."""
    frame = _frame(10)
    got = P.closed_bars(frame, frame[-1].ts + dt.timedelta(minutes=15), 15)
    closes = [c for _, _, c in got]
    assert closes == sorted(closes)


def test_the_repair_window_is_bounded():
    """After an outage the honest thing is to repair the last few hours, not to
    replay a week of stale conditions into the ledger as if they had just
    fired."""
    frame = _frame(200)
    got = P.closed_bars(frame, frame[-1].ts + dt.timedelta(minutes=15), 15, limit=16)
    assert len(got) == 16
    assert got[-1][1] is frame[-1]          # bounded from the NEWEST end


def test_the_newest_bar_is_the_same_decision_either_way():
    """`evaluate_latest` is now a wrapper. If the two paths could disagree, a
    bar would be judged differently depending on whether the producer happened
    to be awake for it — which is exactly the bug this fixes."""
    frame = _frame()
    now = frame[-1].ts + dt.timedelta(minutes=15)
    spec = G.RulesSpec(RUN43)
    idx, bar, closed = P.latest_closed_bar(frame, now, 15)
    a = P.evaluate_latest(spec, _Provider(), frame, _ctx(50), now)
    b = P.evaluate_at(spec, _Provider(), frame, idx, closed, _ctx(50))
    assert a["reason"] == b["reason"] and a["direction"] == b["direction"]
    assert a["closed_at"] == b["closed_at"]


def test_a_repaired_bar_is_still_subject_to_the_caps():
    """Catching up must not become a way around the cooldown."""
    spec = G.RulesSpec(RUN43)               # cooldown 4 bars x 15m = 60 min
    frame = _frame()
    idx, bar, closed = P.latest_closed_bar(
        frame, frame[-1].ts + dt.timedelta(minutes=15), 15)
    recent = closed - dt.timedelta(minutes=30)
    out = P.evaluate_at(spec, _Provider(), frame, idx, closed, _ctx(50),
                        last_signal_at=recent)
    assert out["signal"] is None and out["reason"] == "n_suppressed_cooldown"


def test_warmup_still_applies_to_an_old_bar():
    frame = _frame(40)
    out = P.evaluate_at(G.RulesSpec(RUN43), _Provider(), frame, 3,
                        frame[3].ts + dt.timedelta(minutes=15), _ctx(50))
    assert out["signal"] is None and out["reason"] == "warmup"
