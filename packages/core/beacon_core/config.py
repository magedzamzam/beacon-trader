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

        # Secret used to encrypt credentials/AI keys stored in the DB
        # (see beacon_core.crypto). Falls back to API_TOKEN so an existing
        # deployment keeps working, but a dedicated SECRET_KEY is recommended.
        self.secret_key: str = os.getenv("SECRET_KEY", "") or os.getenv("API_TOKEN", "")

        # AI integrations (Anthropic Claude). Key can also be stored, encrypted,
        # in the settings table from the UI; this env var is the default source.
        self.anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
        self.ai_default_model: str = os.getenv("AI_DEFAULT_MODEL", "claude-opus-4-8")

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
CH_TG_CONTROL = "telegram.control"       # backfill / reload requests to telegram
HEARTBEAT_PREFIX = "hb:"                  # hb:<service> -> unix ts
EXEC_QUEUE = "queue:exec"                # paced broker-call queue

# --- working-order time-to-live (per channel, see #40) --------------------
# Every LIMIT/STOP entry gets a broker-enforced expiry (goodTillDate) so an
# unfilled order can never rest as GTC and fill hours later at a stale price.
# The value is per-channel (sources.strategy.entry_ttl_minutes) but clamped to
# [MIN, MAX] with a safe default so a channel can't be set to GTC by accident.
DEFAULT_ENTRY_TTL_MIN = 60
MIN_ENTRY_TTL_MIN = 1
MAX_ENTRY_TTL_MIN = 480                   # 8h — the slowest-fill channel we allow


def effective_entry_ttl_min(strategy) -> int:
    """The clamped, per-channel working-order TTL in minutes. Falls back to
    DEFAULT_ENTRY_TTL_MIN when unset/invalid and is bounded to [MIN, MAX]."""
    try:
        v = int((strategy or {}).get("entry_ttl_minutes", DEFAULT_ENTRY_TTL_MIN))
    except (TypeError, ValueError):
        v = DEFAULT_ENTRY_TTL_MIN
    return max(MIN_ENTRY_TTL_MIN, min(MAX_ENTRY_TTL_MIN, v))
