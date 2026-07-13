from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from beacon_core.db.base import init_models
from beacon_core.logging import get_logger
from .routers import (accounts, ai, analysis, analytics, auth, brokers, dashboard,
                      entry_filters, events, health, legs, market, messages,
                      notifications, performance, planner, reconciliation, risk, signals,
                      sources, symbols, ta, trades, trading_hours)

log = get_logger("api")


async def _bootstrap_admin():
    """Optionally create an initial admin from ADMIN_USERNAME/ADMIN_PASSWORD env
    when no users exist yet, so the portal has a login out of the box."""
    import os
    from sqlalchemy import func, select
    from beacon_core.db.base import Session
    from beacon_core.db.models import User
    from beacon_core.security import hash_password

    username = os.getenv("ADMIN_USERNAME")
    password = os.getenv("ADMIN_PASSWORD")
    if not (username and password):
        return
    async with Session()() as s:
        count = int((await s.execute(select(func.count(User.id)))).scalar() or 0)
        if count == 0:
            s.add(User(username=username, password_hash=hash_password(password), is_admin=True))
            await s.commit()
            log.info("bootstrapped admin user '%s'", username)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_models()
    try:
        await _bootstrap_admin()
    except Exception as exc:            # never block API startup on bootstrap
        log.warning("admin bootstrap skipped: %s", exc)
    log.info("api ready")
    yield


app = FastAPI(title="Beacon Trader API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

for r in (auth.router, health.router, dashboard.router, brokers.router,
          accounts.router, symbols.router, sources.router, signals.router,
          trades.router, legs.router, market.router, performance.router,
          messages.router, events.router, ai.router, ta.router,
          analysis.router, trading_hours.router, notifications.router,
          reconciliation.router, risk.router, planner.router, entry_filters.router,
          analytics.router):
    app.include_router(r)


@app.get("/")
async def root():
    return {"service": "beacon-trader-api", "version": "0.1.0"}
