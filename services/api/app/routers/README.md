# `routers/` — API endpoints

One FastAPI `APIRouter` per resource, all registered in `app/main.py`. Each is
thin: it validates input, calls into `beacon_core`, and returns JSON. Auth is the
shared `require_token` dependency; DB access is the `get_db` async session.

| Router | Prefix | Notes |
|--------|--------|-------|
| `signals.py` | `/signals` | List; manual ingest; TradingView/webhook ingest; re-initiate. |
| `_ingest.py` | — | Thin wrapper over `beacon_core.ingest.ingest_message` (shared with Telegram). |
| `trades.py` `legs.py` | `/trades` `/legs` | Ledger + per-leg actions (close/cancel). |
| `events.py` | `/events` | Append-only execution/audit log. |
| `reconciliation.py` | `/reconciliation` | Channel-claimed vs bot-actual, with date range. |
| `performance.py` `dashboard.py` | `/performance` `/dashboard` | KPIs, per-source stats, equity curve. |
| `brokers.py` `accounts.py` | `/brokers` `/accounts` | Broker CRUD, live-account fetch, per-broker health. |
| `sources.py` `symbols.py` `market.py` | | Source/symbol config; live quotes/bars. |
| `risk.py` `entry_filters.py` | `/risk-limits` `/entry-filters` | Risk-limit + trend-filter config. |
| `ta.py` `ai.py` `notifications.py` `trading_hours.py` | | Feature config surfaces. |
| `analytics.py` | `/analytics` | Sidecar config, correlation report, structure map/config/recompute/outcome. |
| `analysis.py` | `/analysis` | Bayesian win-rate table + P(win) scores. |
| `messages.py` | `/messages` | Telegram message history + channels + sync. |
| `health.py` | `/health` | DB/Redis/worker heartbeats + TTL-cached broker probe. |
| `auth.py` | `/auth` | Token status / login. |

Full descriptions live in the parent `services/api/README.md`.
