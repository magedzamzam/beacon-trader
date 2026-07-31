"""Debounce for burst-prone notifications (#180).

Broker failures do not arrive alone. A bad epic or a closed market rejects every
leg of a fanout inside the same second; a dropped session rejects every trade on
every tick until it is restored. One alert per rejection turns the event an
operator most needs pushed to their phone into the one they mute — so a burst
must collapse into a single message that says how much it stood for.

Pure and in-process: no DB, no clock beyond `time.monotonic`, imports cleanly
anywhere (like `config` and `templates`). Per-process state means N replicas can
each send one alert per window, which is the safe direction to err.
"""
from __future__ import annotations

import time

DEFAULT_WINDOW_SEC = 300.0      # one alert per key per 5 minutes
_MAX_KEYS = 512                 # prune guard — keys are bounded in practice


class Throttle:
    """One notification per `key` per window; the rest are counted, not sent."""

    def __init__(self, window_sec: float = DEFAULT_WINDOW_SEC):
        self.window_sec = float(window_sec)
        self._last: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}

    def allow(self, key: str, now: float | None = None) -> tuple[bool, int]:
        """`(send?, suppressed)`. The first call for a key sends and reports 0.
        Calls inside the window are counted and stay silent. The first call after
        the window sends again and reports how many it swallowed — so the alert
        can say "+7 more" instead of pretending the burst was one event."""
        now = time.monotonic() if now is None else now
        last = self._last.get(key)
        if last is not None and (now - last) < self.window_sec:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return False, 0
        if len(self._last) >= _MAX_KEYS:
            self._prune(now)
        self._last[key] = now
        return True, self._suppressed.pop(key, 0)

    def _prune(self, now: float) -> None:
        """Drop keys whose window has long elapsed — they carry no suppressed
        count worth reporting, so forgetting them only costs a fresh alert."""
        stale = [k for k, t in self._last.items()
                 if (now - t) >= self.window_sec and not self._suppressed.get(k)]
        for k in stale:
            self._last.pop(k, None)


def suffix(detail: str, suppressed: int) -> str:
    """Append the burst count to a detail line, if there was one."""
    return f"{detail}  (+{suppressed} more in the last few minutes)" if suppressed else detail
