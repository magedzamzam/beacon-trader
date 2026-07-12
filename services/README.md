# `services/` — the runtime processes

Four containers, each a thin process wrapper around `beacon_core`. Each has its
own `Dockerfile` and `requirements.txt`, bakes in the shared library, and runs a
`/healthz` server. They communicate through **Redis** (durable queue + pub/sub +
heartbeats) and share **PostgreSQL** (the ledger).

| Service | Entry | Role |
|---------|-------|------|
| `api/` | `uvicorn app.main:app` | FastAPI: all CRUD, ingest webhooks (TradingView/manual/API), messages, events, dashboard, health, and every settings/analytics endpoint. Serves the frontend's data. |
| `telegram/` | `python main.py` | Telethon **user** session. Persists *every* watched-channel message, runs it through the ingest pipeline, backfills history, and responds to control requests. |
| `executor/` | `python main.py` | Consumes validated signals off the durable queue; plans the fanout, sizes each leg, runs the trust/risk/trend/AI gates, and places orders. Also re-drives stranded signals and captures TA/analytics in the background. |
| `monitor/` | `python main.py` | Loops every `MONITOR_INTERVAL`s: reconciles against the broker, detects TP/SL closes, applies SL-move rules, expires working orders, runs AI outcome analysis, and fires the weekly structure recompute. |

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
