from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from beacon_core.db.base import init_models
from beacon_core.logging import get_logger
from .routers import (accounts, brokers, dashboard, health, legs, market,
                      performance, signals, sources, symbols, trades)

log = get_logger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_models()
    log.info("api ready")
    yield


app = FastAPI(title="Beacon Trader API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

for r in (health.router, dashboard.router, brokers.router, accounts.router,
          symbols.router, sources.router, signals.router, trades.router,
          legs.router, market.router, performance.router):
    app.include_router(r)


@app.get("/")
async def root():
    return {"service": "beacon-trader-api", "version": "0.1.0"}
