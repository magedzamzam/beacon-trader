# `api/` — FastAPI backend

The HTTP surface for the whole platform: everything the frontend reads/writes,
plus the inbound webhooks. Single-user token auth (`API_TOKEN`).

## Structure
| Path | Purpose |
|------|---------|
| `app/main.py` | App factory; registers every router; runs `init_models()` + `seed` on startup. |
| `app/auth.py` | `require_token` dependency (bearer token). |
| `app/deps.py` | `get_db` async-session dependency. |
| `app/schemas.py` | Pydantic request/response models. |
| `app/seed.py` | Idempotent seed of default settings (risk limits, SL rules, entry filter, analytics, structure) on startup. |
| `app/routers/` | One router per resource — see below. |

## Routers (`app/routers/`)
- **Ingest & signals:** `_ingest.py` (shared pipeline wrapper), `signals.py`
  (manual + TradingView/webhook ingest, list, re-initiate), `messages.py`.
- **Trading ledger:** `trades.py`, `legs.py`, `positions` (via trades/legs),
  `events.py`, `reconciliation.py`, `performance.py`, `dashboard.py`.
- **Config/CRUD:** `brokers.py`, `accounts.py`, `sources.py`, `symbols.py`,
  `risk.py`, `entry_filters.py`, `ta.py`, `ai.py`, `notifications.py`,
  `trading_hours.py`, `market.py`.
- **Analytics (shadow):** `analytics.py` (sidecar config, correlation, structure
  map/config/recompute/outcome), `analysis.py` (Bayesian).
- **Ops:** `health.py` (DB/Redis/worker heartbeats + TTL-cached broker probe),
  `auth.py`.

## Notes
- The broker health probe is **TTL-cached** and keeps a persistent per-broker
  session so the dashboard's frequent `/health` polls don't hammer Capital.com's
  rate-limited `/session` endpoint.
- Ingest webhooks feed the same `beacon_core.ingest` pipeline the Telegram
  service uses — one code path, one dedupe strategy.
