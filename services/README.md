# `services/` — the runtime processes

Four **trading** containers, each a thin process wrapper around `beacon_core`.
Each has its own `Dockerfile` and `requirements.txt`, bakes in the shared
library, and runs a `/healthz` server. They communicate through **Redis**
(durable queue + pub/sub + heartbeats) and share **PostgreSQL** (the ledger).

`replay/` is a fifth directory and is deliberately none of those things — see
below the table.

| Service | Entry | Role |
|---------|-------|------|
| `api/` | `uvicorn app.main:app` | FastAPI: all CRUD, ingest webhooks (TradingView/manual/API), messages, events, dashboard, health, and every settings/analytics endpoint. Serves the frontend's data. |
| `telegram/` | `python main.py` | Telethon **user** session. Persists *every* watched-channel message, runs it through the ingest pipeline, backfills history, and responds to control requests. |
| `executor/` | `python main.py` | Consumes validated signals off the durable queue; plans the fanout, sizes each leg, runs the trust/risk/trend/AI gates, and places orders. Also re-drives stranded signals and captures TA/analytics in the background. |
| `monitor/` | `python main.py` | Loops every `MONITOR_INTERVAL`s: reconciles against the broker, detects TP/SL closes, applies SL-move rules, expires working orders, runs AI outcome analysis, and fires the weekly structure recompute. |

### `replay/` — research, not a runtime process (#169)

The offline signal-replay + backtest harness. Every difference from the four
above is the isolation the operator asked for, and each is enforced by a test in
`services/replay/tests/test_isolation.py` rather than by convention:

* **A batch job, not a daemon.** No loop, no `/healthz`, no queue consumer. It
  runs behind a compose `research` profile — `docker compose up -d` does not
  start it — and is invoked explicitly:
  `docker compose run --rm replay python main.py run --config runs/…json`.
  So "stopping replay leaves trading unaffected" is true by construction.
* **No broker credentials.** An explicit `environment:` block instead of
  `env_file: .env`, and an import-graph test proving no order-placing symbol is
  reachable from its entrypoint.
* **No Redis.** It never touches the bus, the durable queue or `CH_SIGNAL_VALID`.
* **Read-only Postgres**, via its own `REPLAY_DATABASE_URL` SELECT-only role. It
  writes only `replay_runs` / `replay_results`, on its own declarative base.

It imports `beacon_core`'s pure engines so it simulates the real bot rather than
a second one. The one-way rule is absolute: **`beacon_core` and the four trading
services must never import `replay`.** See `services/replay/README.md`.

## Data flow
```
telegram / api(ingest) ──publish──▶ Redis queue ──consume──▶ executor ──▶ broker
                                                                  │
                                                                  ▼
                                                              PostgreSQL  ◀── monitor (reconcile + SL rules)
                                                                  ▲
                                                              api ──serves──▶ frontend
```

Each service imports the same adapters, models, and helpers from `beacon_core`,
so a domain change is made once and rebuilt into all images.
