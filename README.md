# Beacon Trader

A self-hosted, containerized portal that turns trading signals (Telegram,
TradingView, manual, API) into risk-sized orders on a broker, monitors the open
positions, and moves stops by rule. Phase 1 targets **GOLD (XAUUSD)** but the
symbol layer is built to extend.

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
| `api`      | FastAPI: CRUD, ingest webhooks (TradingView/manual/API), dashboard, health |
| `telegram` | Telethon user session; detects, parses, validates, publishes signals |
| `executor` | Consumes validated signals; plans the fanout, sizes each leg, places orders |
| `monitor`  | Reconciles vs broker; detects TP/SL closes; applies SL-move rules; TTLs |
| `frontend` | React + Vite + nginx; dark/light trading terminal UI |

PostgreSQL is **external/managed** — you provide `DATABASE_URL` at install.

The broker gateway is a clean adapter interface (`beacon_core.brokers.base`).
Capital.com ships in the box; adding a broker is one class + a registry entry.

---

## The fanout + risk model

One signal becomes **N legs**:

```
legs = (distinct entry levels) × (tp_strategy tokens)
```

`tp_strategy` is a per-source template, e.g. `"tp1, tp1, tp2, tp3"`. Each token
is one leg targeting that TP; repeating `tp1` is how you weight the
high-probability target. A single entry with `tp1,tp2,tp3` → 3 legs; a **range
entry** (`entry_from ≠ entry_to`) doubles it to 6.

**Worked example** (verified in `packages/core`): `BUY 4105-4102, TP 4110/4112/4114,
SL 4098`, template `tp1,tp1,tp2,tp3`, MARKET at 4104.5 → **8 legs**.

**Risk** is two independent choices (see `beacon_core.risk.sizing`):

- **basis** — how the per-signal budget is set:
  - `capital_percent` → budget = equity × value/100
  - `fixed_cash` → budget = an exact dollar amount you're willing to lose
- **allocation** — how it spreads across legs:
  - `even` → each leg risks budget / N
  - `per_tp` → each leg risks `equity × per_tp_percent[tp_index]/100`
    (mirrors the old `tpN_capital_risk_percent`)

`lot = risk_cash / (|entry − sl| × value_per_point)`, rounded down to `lot_step`;
legs below `min_lot` are dropped, not over-risked.

> **Calibrate `value_per_point`.** It is money per 1.0 price move per 1.0 broker
> size, stored on the symbol map. It is the one number that makes real-money
> sizing correct. The seed uses `1` as a placeholder — set it to your broker's
> actual gold contract value before trading real funds.

> **`per_tp` + range entries multiply exposure.** Each entry leg takes the full
> per-TP risk, so a 2-entry signal with `4%/2%/1.5%` risks ~23% of equity if
> everything hits SL. Use `even`, trim the template, or lower the percentages if
> that's more than you intend. The dashboard shows worst-case risk per trade.

---

## Stop-loss rules

Declarative per source (`strategy.sl_rules`), evaluated by the monitor off the
**live price** (so a reversal is acted on without waiting for a fill confirm):

```json
{"trigger": {"type": "tp_hit", "index": 1},
 "action":  {"type": "move_sl_to", "target": "entry"}}
{"trigger": {"type": "price_move", "points": 3},
 "action":  {"type": "move_sl_to", "target": "number", "value": 4102}}
```

Two actions: **move SL to entry**, **move SL to a number**. Stops only ever
tighten toward profit — a rule can never loosen a stop.

---

## Order policy (Phase 1)

- **MARKET** strategy → order sent at market.
- **LIMIT** strategy → limit orders only (never stop orders). Unfilled entries
  are cancelled after `entry_ttl_minutes` (default 60).
- A TP that violates the broker's minimum distance drops **only that leg**.

---

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
packages/core/beacon_core/   shared library (installed into each image)
  brokers/    adapter base + types + registry + capital_com
  parsing/    symbol registry + signal parser
  execution/  fanout planner
  risk/       position sizing
  strategy/   SL-rule engine
  db/         async engine + schema
services/     api · telegram · executor · monitor  (each: Dockerfile + code)
frontend/     React + Vite + nginx
scripts/      init_db.py
```

See **INSTALL.md** for the deployment runbook.
