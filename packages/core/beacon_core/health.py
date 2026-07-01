"""Tiny HTTP health endpoint for worker services (executor, monitor, telegram).
Runs alongside the worker's main loop so Docker healthchecks have something to
curl. Also emits a Redis heartbeat each tick."""
from __future__ import annotations

import asyncio
from aiohttp import web

from .bus import Bus
from .logging import get_logger

log = get_logger("health")


async def run_health_server(service: str, bus: Bus, port: int = 8080) -> None:
    async def handler(_request):
        return web.json_response({"ok": True, "service": service})

    app = web.Application()
    app.router.add_get("/health", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("health server for %s on :%s", service, port)
    while True:
        try:
            await bus.beat(service)
        except Exception as exc:                 # never let heartbeat kill worker
            log.warning("heartbeat failed: %s", exc)
        await asyncio.sleep(5)
