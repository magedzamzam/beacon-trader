# `packages/core` — the shared library

`beacon_core` is the one Python package every service depends on. It is **baked
into each service image at build time** (see each `services/*/Dockerfile`), so a
change here requires rebuilding the `api`, `executor`, `monitor`, and `telegram`
images before it takes effect in a deployment.

## Why a shared package
The four services are thin process wrappers around a large, shared body of
domain logic: broker adapters, signal parsing, risk sizing, the SL-rule engine,
the DB schema, the AI layer, and the analytics sidecar. Putting all of that in
one installable package means there is **one source of truth** — the executor
that sizes an order and the monitor that reconciles it use the exact same
adapter, models, and helpers.

## Layout
| Path | What it is |
|------|------------|
| `beacon_core/` | The library itself — see its README for the subpackage map. |
| `tests/` | Pure-Python unit tests for the library (no DB/broker needed). |
| `pyproject.toml` | Package metadata; installed editable/wheel into each image. |

## Design rules that hold throughout
- **Pure where possible.** Estimators, parsers, sizing, SL rules, and the
  structure engine are pure functions of their inputs so they unit-test on a
  bare box (no Postgres/Redis). DB/broker imports are deferred into the
  functions that need them.
- **Shadow analytics never gate trading.** Anything under `analysis/` is
  observability; it can fail, time out, or be disabled without affecting an
  order (measure-before-gate).
- **Broker is the source of truth.** The DB is a ledger reconciled against the
  broker; adapters normalize every broker into the same typed contracts.
