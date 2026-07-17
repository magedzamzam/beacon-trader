from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from beacon_core.db.base import init_models
from beacon_core.logging import get_logger
from .routers import (accounts, ai, analysis, analytics, auth, brokers, dashboard,
                      events, health, legs, market, messages,
                      notifications, performance, reconciliation, risk, signals,
                      sources, strategies, symbols, ta, trades, trading_hours)

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


async def _migrate_global_entry_config():
    """#104: Strategies is the single source of truth for the entry filter + chase
    guard. Lift the legacy GLOBAL `entry_filters` / `planner` settings into the
    (Any, Any) ExecutionStrategy base row, so retiring those settings from the
    executor changes nothing behaviourally.

    Idempotent and non-destructive: only fills a pillar that is still NULL, so an
    operator edit is never clobbered and re-running is a no-op."""
    from sqlalchemy import select
    from beacon_core.db.base import Session
    from beacon_core.db.models import ExecutionStrategy
    from beacon_core.execution.planner import DEFAULT_PLANNER
    from beacon_core.execution.strategy import ENTRY_POLICY_KEYS
    from beacon_core.config import effective_entry_ttl_min
    from beacon_core.settings_store import get_setting

    async with Session()() as s:
        base = (await s.execute(select(ExecutionStrategy).where(
            ExecutionStrategy.account_id.is_(None),
            ExecutionStrategy.source_id.is_(None)))).scalar_one_or_none()
        if base is not None and base.entry_policy and base.entry_filters:
            return                                  # already migrated

        merged = {**DEFAULT_PLANNER, **(await get_setting(s, "planner", {}) or {})}
        entry_policy = {k: merged[k] for k in ENTRY_POLICY_KEYS if k in merged}
        entry_policy["ttl_minutes"] = effective_entry_ttl_min({})     # clamped default (60)
        stored_filters = await get_setting(s, "entry_filters", None)
        entry_filters = dict(stored_filters) if stored_filters else None

        if base is None:
            s.add(ExecutionStrategy(account_id=None, source_id=None,
                                    entry_policy=entry_policy, entry_filters=entry_filters,
                                    label="Global default",
                                    note="Migrated from the legacy global entry_filters/planner settings (#104)"))
            log.info("MIGRATION #104: created the (Any, Any) base strategy from global settings")
        else:
            if base.entry_policy is None:
                base.entry_policy = entry_policy
            if base.entry_filters is None and entry_filters:
                base.entry_filters = entry_filters
            log.info("MIGRATION #104: filled the (Any, Any) base strategy from global settings")
        await s.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_models()
    try:
        await _bootstrap_admin()
    except Exception as exc:            # never block API startup on bootstrap
        log.warning("admin bootstrap skipped: %s", exc)
    try:
        await _migrate_global_entry_config()
    except Exception as exc:            # never block API startup on migration
        log.warning("entry-config migration skipped: %s", exc)
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
          reconciliation.router, risk.router,
          analytics.router, strategies.router):
    app.include_router(r)


@app.get("/")
async def root():
    return {"service": "beacon-trader-api", "version": "0.1.0"}
