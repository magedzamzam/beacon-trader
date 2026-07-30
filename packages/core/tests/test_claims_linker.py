"""Claim linker resilience (#173).

One message that raised used to discard the whole pass, so the high-water mark
was never written and the next run failed at the same place — a permanent wedge.
It cost three days of claims (1,574 messages unscanned from 2026-07-27) with no
log line anywhere, because both call sites were `except Exception: pass` and the
only log was gated on success.

These pin the isolation: a bad message costs ONE claim, never every claim after
it, and the mark always advances over what was scanned. DB-free — the session
and settings are faked, so it runs on a bare box like everything else here.
"""
import asyncio
from types import SimpleNamespace as S

from beacon_core.analysis import claims as C


class _Res:
    """Stands in for a SQLAlchemy Result."""
    def __init__(self, rows=None, rowcount=1):
        self._rows, self.rowcount = rows or [], rowcount

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Savepoint:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        self.session.savepoints += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.session.rollbacks += 1
        return False                      # never swallow — link_claims must see it


class FakeSession:
    """`msgs` are returned by the first execute(); every later execute() is an
    insert, which raises for any message id in `poison`."""
    def __init__(self, msgs, poison=()):
        self.msgs, self.poison = msgs, set(poison)
        self.inserted, self.commits = [], 0
        self.savepoints = self.rollbacks = 0
        self._first = True
        self._current = None

    async def execute(self, stmt):
        if self._first:                   # the message scan
            self._first = False
            return _Res(self.msgs)
        m = self._current
        if m is not None and m.id in self.poison:
            raise RuntimeError("boom on message %s" % m.id)
        if m is not None:
            self.inserted.append(m.id)
        return _Res(rowcount=1)

    def begin_nested(self):
        return _Savepoint(self)

    async def commit(self):
        self.commits += 1


def _msg(mid, text="TP1 HIT", sig=None):
    return S(id=mid, text=text, source_id=1, message_date=None,
             reply_to_message_id=None, chat_id=1, signal_id=sig)


def _run(monkeypatch, msgs, poison=(), hwm=0, resolves=True):
    """Drive link_claims over `msgs`, faking settings and signal resolution."""
    sess = FakeSession(msgs, poison)
    stored = {"hwm": hwm}

    async def _get(_s, key, default=0):
        return stored.get("hwm", default)

    async def _set(_s, key, val):
        stored["hwm"] = val

    async def _resolve(_s, m, _h):
        sess._current = m                 # so the fake insert knows which message
        return (99, 1.0) if resolves else (None, None)

    monkeypatch.setattr(C, "get_setting", _get)
    monkeypatch.setattr(C, "set_setting", _set)
    monkeypatch.setattr(C, "_resolve_signal", _resolve)
    monkeypatch.setattr(C, "parse_outcome",
                        lambda t: {"max_tp": 1, "sl_hit": False, "all_tp": False} if t else None)
    out = asyncio.run(C.link_claims(sess))
    return out, sess, stored


def test_a_poison_message_no_longer_blocks_the_ones_after_it(monkeypatch):
    """THE regression. Message 2 raises; 1 and 3 must still be claimed."""
    out, sess, stored = _run(monkeypatch, [_msg(1), _msg(2), _msg(3)], poison=[2])
    assert sess.inserted == [1, 3]
    assert out["added"] == 2 and out["skipped"] == 1
    assert stored["hwm"] == 3             # advanced PAST the poison message


def test_the_high_water_mark_advances_even_when_every_message_fails(monkeypatch):
    """The wedge: if the mark does not move, the next pass re-reads the same
    messages and fails identically, forever."""
    out, _sess, stored = _run(monkeypatch, [_msg(1), _msg(2)], poison=[1, 2])
    assert out["added"] == 0 and out["skipped"] == 2
    assert stored["hwm"] == 2


def test_failures_are_reported_not_swallowed(monkeypatch):
    out, _s, _h = _run(monkeypatch, [_msg(1), _msg(2)], poison=[2])
    assert len(out["errors"]) == 1
    assert out["errors"][0]["message_id"] == 2
    assert "boom" in out["errors"][0]["error"]


def test_error_list_is_capped_but_the_count_is_not(monkeypatch):
    n = C.MAX_REPORTED_ERRORS + 5
    msgs = [_msg(i) for i in range(1, n + 1)]
    out, _s, _h = _run(monkeypatch, msgs, poison=range(1, n + 1))
    assert out["skipped"] == n                       # every failure counted
    assert len(out["errors"]) == C.MAX_REPORTED_ERRORS   # ...but the payload is bounded


def test_each_message_gets_its_own_savepoint(monkeypatch):
    """try/except alone is not enough: a DB error poisons the transaction, so the
    isolation has to be a real SAVEPOINT or the next insert fails too."""
    _out, sess, _h = _run(monkeypatch, [_msg(1), _msg(2), _msg(3)], poison=[2])
    assert sess.savepoints == 3
    assert sess.rollbacks == 1


def test_a_clean_pass_still_behaves(monkeypatch):
    out, sess, stored = _run(monkeypatch, [_msg(1), _msg(2)])
    assert out == {"scanned": 2, "added": 2, "skipped": 0, "errors": [], "hwm": 2}
    assert sess.commits == 1 and sess.rollbacks == 0
    assert stored["hwm"] == 2


def test_unresolvable_messages_are_skipped_without_counting_as_errors(monkeypatch):
    """A follow-up we cannot tie to a signal is normal, not a failure."""
    out, sess, stored = _run(monkeypatch, [_msg(1), _msg(2)], resolves=False)
    assert out["added"] == 0 and out["skipped"] == 0 and out["errors"] == []
    assert sess.inserted == []
    assert stored["hwm"] == 2             # still scanned, so still advances


def test_non_outcome_messages_never_touch_the_database(monkeypatch):
    out, sess, stored = _run(monkeypatch, [_msg(1, text=""), _msg(2, text="")])
    assert out["added"] == 0 and out["skipped"] == 0
    assert sess.savepoints == 0           # no DB work attempted at all
    assert stored["hwm"] == 2
