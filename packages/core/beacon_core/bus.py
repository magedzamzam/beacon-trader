"""Thin async Redis wrapper: pub/sub bus + a paced work queue + heartbeats."""
from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator, Optional

import redis.asyncio as aioredis
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from .config import HEARTBEAT_PREFIX, get_settings
from .logging import get_logger

log = get_logger("bus")

# transient failures a long-lived consumer should reconnect through, not die on.
# NB: RedisTimeoutError is a subclass of RedisError but is NOT an outage on a
# blocking read (BRPOP) — it's a normal idle poll, so it's handled separately
# (see consume_queue, #71) and must be caught BEFORE this tuple.
_TRANSIENT = (RedisError, OSError)


class Bus:
    def __init__(self, url: Optional[str] = None) -> None:
        self._url = url or get_settings().redis_url
        self._r: Optional[aioredis.Redis] = None

    @property
    def r(self) -> aioredis.Redis:
        if self._r is None:
            # keepalive + periodic health checks so a dead/stale connection (e.g.
            # after a Redis restart or a brief AOF-fsync stall) is detected and
            # pruned quickly instead of hanging reads; short connect timeout so a
            # reconnect fails fast into the backoff loop.
            self._r = aioredis.from_url(
                self._url, decode_responses=True,
                socket_keepalive=True, health_check_interval=30,
                socket_connect_timeout=5)
        return self._r

    async def publish(self, channel: str, payload: dict) -> None:
        await self.r.publish(channel, json.dumps(payload, default=str))

    async def subscribe(self, channel: str) -> AsyncIterator[dict]:
        pubsub = self.r.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    yield json.loads(msg["data"])
                except (ValueError, TypeError):
                    continue
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception:                       # connection may already be dead
                pass

    async def _reset(self) -> None:
        """Drop the cached client so the next call reconnects cleanly."""
        old, self._r = self._r, None
        if old is not None:
            try:
                await old.aclose()
            except Exception:
                pass

    async def subscribe_forever(self, channel: str) -> AsyncIterator[dict]:
        """Pub/sub consumer that self-heals on Redis drops (at-most-once; for
        control / non-critical channels). Never exits on a transient error."""
        backoff = 1
        while True:
            try:
                async for msg in self.subscribe(channel):
                    backoff = 1
                    yield msg
            except _TRANSIENT as exc:
                log.warning("bus subscribe %s: reconnecting in %ss (%s)", channel, backoff, exc)
                await self._reset()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)

    # --- work queue (LPUSH / BRPOP) — durable, at-least-once ---
    async def enqueue(self, key: str, payload: dict) -> None:
        await self.r.lpush(key, json.dumps(payload, default=str))

    async def dequeue(self, key: str, timeout: int = 5) -> Optional[dict]:
        res = await self.r.brpop(key, timeout=timeout)
        if not res:
            return None
        try:
            return json.loads(res[1])
        except (ValueError, TypeError):
            return None

    async def consume_queue(self, key: str, timeout: int = 5) -> AsyncIterator[dict]:
        """Durable, self-healing consumer over a BRPOP list. At-least-once: a
        message waits in the list across consumer downtime / restarts and is
        delivered on return; Redis blips reconnect with exponential backoff
        instead of killing the loop. Pair with an idempotent handler."""
        backoff = 1
        while True:
            try:
                msg = await self.dequeue(key, timeout=timeout)
                backoff = 1
                if msg is not None:
                    yield msg
            except RedisTimeoutError:
                # Idle BRPOP read-timeout on an empty queue — a normal empty poll,
                # NOT a dropped connection (#71). Keep polling immediately; do not
                # reset the client or back off (that added up to ~10s of latency
                # and log noise). A signal enqueued next is consumed at once.
                backoff = 1
                continue
            except _TRANSIENT as exc:                 # genuine connection loss
                log.warning("bus consume %s: reconnecting in %ss (%s)", key, backoff, exc)
                await self._reset()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)

    # --- heartbeats ---
    async def beat(self, service: str) -> None:
        await self.r.set(f"{HEARTBEAT_PREFIX}{service}", int(time.time()))

    async def last_beat(self, service: str) -> Optional[int]:
        v = await self.r.get(f"{HEARTBEAT_PREFIX}{service}")
        return int(v) if v else None

    async def aclose(self) -> None:
        if self._r is not None:
            await self._r.aclose()
