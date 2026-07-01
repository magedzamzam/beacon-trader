"""Environment-driven settings, shared by every service."""
from __future__ import annotations

import os
from functools import lru_cache


class Settings:
    def __init__(self) -> None:
        # Managed PostgreSQL (asyncpg driver). Provided at install time.
        self.database_url: str = os.getenv(
            "DATABASE_URL", "postgresql+asyncpg://beacon:beacon@localhost:5432/beacon")
        self.redis_url: str = os.getenv("REDIS_URL", "redis://redis:6379/0")

        # Single-user auth for the portal (Phase 1).
        self.api_token: str = os.getenv("API_TOKEN", "change-me")

        # Telegram (user MTProto session — see services/telegram/login.py).
        self.tg_api_id: str = os.getenv("TG_API_ID", "")
        self.tg_api_hash: str = os.getenv("TG_API_HASH", "")
        self.tg_session: str = os.getenv("TG_SESSION", "")

        # Broker call pacing (requests/sec) to stay under Capital.com limits.
        self.broker_rate_per_sec: float = float(os.getenv("BROKER_RATE_PER_SEC", "3"))

        # Monitor reconcile cadence (seconds).
        self.monitor_interval: float = float(os.getenv("MONITOR_INTERVAL", "5"))

        self.log_level: str = os.getenv("LOG_LEVEL", "INFO")


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Redis channels / keys (single source of truth).
CH_SIGNAL_RAW = "signals.raw"            # detected, pre-parse
CH_SIGNAL_VALID = "signals.validated"    # parsed + validated, ready to trade
CH_TRADE_OPENED = "trades.opened"
CH_TRADE_EVENT = "trades.events"
HEARTBEAT_PREFIX = "hb:"                  # hb:<service> -> unix ts
EXEC_QUEUE = "queue:exec"                # paced broker-call queue
