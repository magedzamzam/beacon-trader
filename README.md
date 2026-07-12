# Beacon Trader

A self-hosted, containerized, **fully configurable** trading platform that turns
signals (Telegram, TradingView, manual, API) into risk-sized orders on a broker,
monitors the open positions, and moves stops by rule. Everything is managed from
the portal: connect broker accounts, watch every Telegram message, see the
signals each channel produced, follow the execution workflow, and (optionally)
have an **AI layer validate signals, executions, and outcomes**. Phase 1 targets
**GOLD (XAUUSD)** but the symbol layer is built to extend.

**Configure it from the frontend:**

- **Brokers & accounts** — add a Capital.com broker with credentials entered in
  the UI (stored **encrypted** in the DB) or referenced from `.env`; fetch the
  live account list with balances and currencies and enable the ones you trade.
- **Message history** — every message on a watched Telegram channel is persisted
  (signal or not) and browsable per channel, with a one-click history backfill.
- **Signals per channel** — filter the signal feed by channel and see which
  messages became signals and trades.
- **Execution workflow** — an append-only activity log of every decision and
  broker interaction, plus per-trade timelines.
- **AI-ready** — enable Anthropic-backed assessment of each signal, a pre-trade
  review of the sized plan (with an optional hard gate), and a post-trade
  outcome analysis. Verdicts are stored and auditable.

> **Read this first.** This is a Phase-1 foundation with real, tested core
> logic — not a finished, audited trading system. It places real orders when
> pointed at a funded account. **Run it on a DEMO account until you have
> watched it behave correctly for days.** Nothing here is financial advice, and
> you are responsible for every order it sends.

---

## Architecture

```
                         ┌─────────────┐
  Telegram channel ─────▶│  telegram   │─┐
  TradingView/API ──────▶│   (api)     │ │  validated signals
  Manual desk    ──────▶ │   (api)     │ │  (Redis pub/sub)
                         └─────────────┘ │
                                         ▼
   ┌───────────┐   places orders   ┌───────────┐
   │  Broker   │◀──────────────────│ executor  │  fanout + risk sizing
   │ (Capital) │──positions/fills─▶│           │
   └───────────┘                   └───────────┘
        ▲                                │ writes
        │ modify SL / reconcile          ▼
   ┌───────────┐                   ┌───────────┐
   │  monitor  │──────────────────▶│ PostgreSQL│  ledger + audit
   └───────────┘                   └───────────┘
                                         ▲
                         ┌───────────┐   │ reads
                         │ frontend  │───┘  dashboard / positions / performance
                         └───────────┘
```

**Services** (each a container, each with a health check):

| Service    | Role |
|------------|------|
| `redis`    | Message bus (pub/sub), paced-queue primitive, service heartbeats |
| `api`      | FastAPI: CRUD, ingest webhooks (TradingView/manual/API), messages, events, AI config, dashboard, health |
| `telegram` | Telethon user session; persists **every** channel message, parses/validates signals, backfills history, AI-validates |
| `executor` | Consumes validated signals; plans the fanout, sizes each leg, optional AI pre-trade review/gate, places orders |
| `monitor`  | Reconciles vs broker; detects TP/SL closes; applies SL-move rules; TTLs; AI outcome analysis on close |
| `frontend` | React + Vite + nginx; dark/light trading terminal UI (Messages, Activity, AI pages) |

PostgreSQL is **external/managed** — you provide `DATABASE_URL` at install.

The broker gateway is a clean adapter interface (`beacon_core.brokers.base`).
Capital.com ships in the box; adding a broker is one class + a registry entry.

---

## The fanout + risk model

One signal becomes **N legs**:

```
legs = (distinct entry levels) × (take-profit levels)
```

One leg per TP per entry — nothing else. A single entry with 3 TPs → 3 legs; a
**range entry** (`entry_from ≠ entry_to`) with 3 TPs → 6 legs. The signal's own
entries and TPs define the shape.

**Risk** is two independent choices (see `beacon_core.risk.sizing`):

- **basis** — how the per-signal budget is set:
  - `capital_percent` → budget = equity × value/100
  - `fixed_cash` → an exact amount you're willing to lose
- **allocation** — how it spreads across legs:
  - `even` → each leg risks budget / N
  - `per_tp` → each leg risks `equity × per_tp_percent[tp_index]/100`

`lot = risk_cash / (|entry − sl| × value_per_point)`, rounded down to `lot_step`.

**Currency is handled dynamically.** The executor reads the account currency
(from the account) and the instrument currency (from the broker's gold market),
then resolves the FX rate from the broker's **own FX market** — no hardcoded
rate — to convert the risk budget before sizing. A USD account needs no
conversion; an AED account is converted at the live rate. If no FX route exists,
that account is skipped (logged `fx_unavailable`) rather than mis-sized.

> **Calibrate `value_per_point`** (money per 1.0 price move per 1.0 size, in the
> instrument's currency) on the symbol map before trading real funds.

---

## Stop-loss rules

Declarative per source (`strategy.sl_rules`), evaluated by the monitor off the
**live price**. Rules chain — each fires independently and the engine applies
whichever tightens the stop most (it never loosens).

Triggers: `tp_hit` (index) or `price_move` (points).
Actions (`move_sl_to`): `entry`, `number` (value), `tp` (index), `previous_tp`.

The classic ratchet:

```
TP1 hit → SL to entry
TP2 hit → SL to previous_tp   (TP1)
TP3 hit → SL to previous_tp   (TP2)
```

---

## Order policy (Phase 1)

- **MARKET** strategy → order sent at market.
- **LIMIT** strategy → limit orders only (never stop orders). Unfilled entries
  are cancelled after `entry_ttl_minutes` (default 60).
- A TP that violates the broker's minimum distance drops **only that leg**.

---

## AI validation layer

Three assessment surfaces, each producing a structured, **auditable** verdict
stored in `ai_assessments` (see `beacon_core.ai`):

- **Signal validation** — as a signal arrives (Telegram / TradingView / manual),
  the model judges coherence, geometry, risk:reward, and red flags →
  `approve | caution | reject` with a confidence and quality score.
- **Execution review** — before the executor places a sized plan on an account,
  the model sanity-checks total risk and lot sizes. With **gate execution** on,
  a `reject` (at/above your confidence threshold) blocks that account's trade
  and logs an `ai_blocked` event.
- **Outcome analysis** — when a trade closes, the model reviews the execution
  and records lessons.

It is **provider-abstracted** (Anthropic Claude today, default `claude-opus-4-8`)
and **degrades gracefully**: with no key or an unreachable API it returns "no
verdict" — the trading path never depends on the AI being up. Enable it and set
the key (env `ANTHROPIC_API_KEY`, or entered encrypted from the **AI** page).

## Configuration & security

- **Secrets at rest.** Broker credentials and the AI key can be entered from the
  UI and are **Fernet-encrypted** with `SECRET_KEY` before being stored
  (`beacon_core.crypto`). You can still reference `.env` variables instead
  (`*_env` keys) — both work side by side. Rotating `SECRET_KEY` makes existing
  encrypted values unreadable, so set it once.
- **Runtime settings.** AI config and feature toggles live in a `settings` table
  editable from the portal — reconfigure without a redeploy.
- **Schema.** New tables (`settings`, `telegram_messages`, `ai_assessments`) are
  created idempotently on startup alongside the existing ledger.

## Honest scope notes

- **Latency.** "Microsecond" execution is not possible through a retail broker
  REST API — round-trips are tens-to-hundreds of ms. The async design, Redis
  bus, and paced queue optimize reaction time *within* that reality; they don't
  beat physics.
- **Fill/close correlation.** For LIMIT legs, exact fill/close prices are
  correlated heuristically over REST in Phase 1. **SL-move decisions run off
  live price and do not depend on this**, so capital protection is unaffected; a
  later phase should read broker `/history` for exact P&L attribution.
- **Broker is source of truth.** The DB is a ledger reconciled against the
  broker each monitor tick.
- **Parser is intentionally simple** and will miss edge cases (abbreviated
  second bounds, exotic formats). It's built to iterate — see
  `beacon_core/parsing`.

---

## Repo layout

```
packages/core/beacon_core/   shared library (baked into each image)
  brokers/       adapter base + types + registry + capital_com + fx
  parsing/       symbol registry + signal & outcome parsers
  ingest/        the one inbound pipeline (parse→validate→dedupe→persist→publish)
  execution/     fanout planner + trust/risk guard + trend-alignment filter
  risk/          position sizing (basis × allocation → lots)
  strategy/      declarative SL-rule ratchet engine
  ta/            indicator registry + per-signal feature capture
  analysis/      shadow analytics: bayes, sidecar estimators, structure/magnet, reports
  ai/            provider + assessments + orchestration (signals/exec/outcome)
  trading_hours/ sessions + holidays + economic-calendar blackout
  notifications/ multi-channel operational alerts (best-effort)
  db/            async engine + full ORM schema
  {config,bus,tasks,timeutil,crypto,settings_store,logging,health}.py   infra
  tests/         pure-Python unit tests (run on a bare box)
services/     api · telegram · executor · monitor  (each: Dockerfile + code)
frontend/     React + Vite + nginx  (Dashboard · Analytics · Brokers · …)
scripts/      init_db.py
```

**Every folder has its own `README.md`** describing its purpose, files, and how it
works — start at `packages/core/beacon_core/README.md` for the library map, or
`services/README.md` for the runtime processes.

See **INSTALL.md** for the deployment runbook.
