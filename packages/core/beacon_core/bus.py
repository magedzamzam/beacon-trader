"""Thin async Redis wrapper: pub/sub bus + a paced work queue + heartbeats."""
from __future__ import annotations

import json
import time
from typing import AsyncIterator, Optional

import redis.asyncio as aioredis

from .config import HEARTBEAT_PREFIX, get_settings


class Bus:
    def __init__(self, url: Optional[str] = None) -> None:
        self._url = url or get_settings().redis_url
        self._r: Optional[aioredis.Redis] = None

    @property
    def r(self) -> aioredis.Redis:
        if self._r is None:
            self._r = aioredis.from_url(self._url, decode_responses=True)
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
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    # --- work queue (LPUSH / BRPOP) ---
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

    # --- heartbeats ---
    async def beat(self, service: str) -> None:
        await self.r.set(f"{HEARTBEAT_PREFIX}{service}", int(time.time()))

    async def last_beat(self, service: str) -> Optional[int]:
        v = await self.r.get(f"{HEARTBEAT_PREFIX}{service}")
        return int(v) if v else None

    async def aclose(self) -> None:
        if self._r is not None:
            await self._r.aclose()
