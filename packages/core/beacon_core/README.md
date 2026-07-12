# `beacon_core` — shared domain library

Every service imports from here. The package groups the platform's domain logic
into focused subpackages plus a handful of top-level infrastructure modules.

## Subpackages
| Package | Purpose |
|---------|---------|
| `brokers/` | Broker-agnostic adapter contract + the Capital.com adapter + FX/symbol/adapter factories. |
| `parsing/` | Turn free-text signals and outcome follow-ups into typed objects; symbol price-band registry. |
| `ingest/` | The single inbound pipeline (parse → validate → dedupe → persist → publish) behind a `BaseInboundChannel`. |
| `execution/` | Fanout planner, live-execution guard + risk-limit brakes, and the trend-alignment entry filter. |
| `risk/` | Position sizing (budget basis × allocation → lots). |
| `strategy/` | Declarative stop-loss ratchet rule engine. |
| `ta/` | Technical-analysis indicator registry + per-signal capture into `signal_features`. |
| `analysis/` | Shadow analytics: Bayesian model, sidecar estimators, market-structure/magnet engine, reconciliation, reports. **Never gates.** |
| `ai/` | Provider-abstracted LLM layer: signal/execution/outcome assessments + orchestration. |
| `trading_hours/` | Session windows, holiday calendar, and economic-calendar news blackout (read-only intelligence). |
| `notifications/` | Multi-channel operational alerts (config, dispatch, senders) — best-effort, never affects trading. |
| `db/` | Async SQLAlchemy engine, session factory, and the full ORM schema (`init_models` create-all). |

## Top-level modules
| Module | Purpose |
|--------|---------|
| `config.py` | Env-driven `Settings` + Redis channel names + the working-order TTL clamp helper. |
| `bus.py` | Thin async Redis wrapper: pub/sub, a durable BRPOP work queue, and service heartbeats. |
| `tasks.py` | `spawn_bg()` — the fire-and-forget task registry that holds a strong ref so background tasks aren't GC'd. |
| `timeutil.py` | `utcnow()` (tz-aware UTC) and `parse_iso_utc()` (UTC-normalizing ISO parse). |
| `crypto.py` | Fernet encrypt/decrypt for secrets at rest (broker creds, AI key), keyed by `SECRET_KEY`. |
| `settings_store.py` | DB-backed runtime settings (`get_setting`/`set_setting`) — reconfigure without redeploy. |
| `security.py` | Auth/token helpers shared by the API. |
| `logging.py` | `get_logger()` — level-based, TTY-colorized log formatter. |
| `health.py` | The shared `/healthz` HTTP server each worker runs, plus heartbeat wiring. |

## The end-to-end flow this library implements
```
inbound msg ─ ingest/ ─▶ parsing/ ─▶ (validate) ─▶ Signal row ─▶ Redis queue
                                                                     │
   executor consumes ─▶ execution/ (plan) ─▶ risk/ (size) ─▶ brokers/ (place)
                                                                     │
   monitor reconciles ─▶ strategy/ (move SL) ─▶ brokers/ (modify) ─▶ db/ ledger
                                                                     │
   (background, non-blocking) ta/ + analysis/ + ai/  ─▶ signal_features / signal_analytics
```
