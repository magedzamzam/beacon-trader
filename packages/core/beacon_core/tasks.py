"""Fire-and-forget background tasks with a strong-ref registry so a task isn't
garbage-collected mid-run. One shared registry across the process (#36) — the
`_BG_TASKS` + create_task + discard-callback dance was copy-pasted in 6 places.
"""
from __future__ import annotations

import asyncio
from typing import Coroutine

_BG: set = set()


def spawn_bg(coro: Coroutine) -> asyncio.Task:
    """Schedule `coro` as a background task, keeping a strong reference until it
    finishes. Returns the task."""
    t = asyncio.create_task(coro)
    _BG.add(t)
    t.add_done_callback(_BG.discard)
    return t
