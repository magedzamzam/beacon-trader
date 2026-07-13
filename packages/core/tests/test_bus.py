"""Durable, self-healing signal consumer (#34). Imports the real bus (needs the
`redis` package), so this runs in CI, not on a bare box."""
import asyncio

from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.exceptions import ConnectionError as RedisConnectionError

from beacon_core.bus import Bus


def _run_consume(bus, seq):
    """Drive consume_queue over `seq` (exceptions raised, values yielded) until
    one message is delivered; return (delivered, reset_count) with no real sleep."""
    st = {"i": 0, "resets": 0}

    async def fake_dequeue(key, timeout=5):
        item = seq[min(st["i"], len(seq) - 1)]
        st["i"] += 1
        if isinstance(item, BaseException):
            raise item
        return item

    async def fake_reset():
        st["resets"] += 1

    bus.dequeue = fake_dequeue
    bus._reset = fake_reset
    orig_sleep = asyncio.sleep
    asyncio.sleep = lambda *_: orig_sleep(0)
    try:
        async def run():
            got = []
            async for msg in bus.consume_queue("k", timeout=0):
                got.append(msg)
                break
            return got
        got = asyncio.run(run())
    finally:
        asyncio.sleep = orig_sleep
    return got, st["resets"]


def test_idle_brpop_timeout_does_not_reconnect():
    # #71: idle BRPOP read-timeouts are normal empty polls, NOT outages — no
    # reset, no backoff; the next enqueued message is delivered at once.
    bus = Bus(url="redis://unused")
    got, resets = _run_consume(
        bus, [RedisTimeoutError("idle"), RedisTimeoutError("idle"), {"signal_id": 9}])
    assert got == [{"signal_id": 9}]
    assert resets == 0                            # timeouts must NOT reconnect


def test_real_connection_loss_still_reconnects():
    bus = Bus(url="redis://unused")
    got, resets = _run_consume(
        bus, [RedisConnectionError("down"), {"signal_id": 3}])
    assert got == [{"signal_id": 3}]
    assert resets == 1                            # a genuine outage still self-heals


def test_consume_queue_reconnects_and_delivers():
    bus = Bus(url="redis://unused")          # no connection is opened (dequeue is stubbed)
    seq = [RedisError("blip"), RedisError("blip2"), {"signal_id": 7}, None]
    st = {"i": 0, "resets": 0}

    async def fake_dequeue(key, timeout=5):
        item = seq[min(st["i"], len(seq) - 1)]
        st["i"] += 1
        if isinstance(item, BaseException):
            raise item
        return item

    async def fake_reset():
        st["resets"] += 1

    bus.dequeue = fake_dequeue
    bus._reset = fake_reset

    orig_sleep = asyncio.sleep
    asyncio.sleep = lambda *_: orig_sleep(0)   # no backoff delay in the test

    async def run():
        got = []
        async for msg in bus.consume_queue("k", timeout=0):
            got.append(msg)
            break
        return got

    try:
        got = asyncio.run(run())
    finally:
        asyncio.sleep = orig_sleep

    assert got == [{"signal_id": 7}]           # message survived the blips
    assert st["resets"] == 2                    # reconnected twice, loop never died
