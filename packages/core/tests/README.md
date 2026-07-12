# `tests/` — library unit tests

Pure-Python unit tests for `beacon_core`. The domain logic is deliberately pure
(functions of their inputs), so most of these run on a **bare box** with no
Postgres/Redis/broker — fast, deterministic, and safe to run anywhere.

## Running
```bash
# from the repo root
PYTHONPATH=packages/core python -m pytest packages/core/tests -q
```
Tests that import the DB/Redis stack (`test_bus`, `test_notifications_dispatch`)
**skip** automatically when those modules aren't installed — they run in CI/Docker
where the full stack is present.

## Coverage map (test → module under test)
| Test | Exercises |
|------|-----------|
| `test_bayes` | `analysis/bayes` (Beta-Binomial + Naive-Bayes) |
| `test_reconcile` | `analysis/reconcile` |
| `test_sidecar` | `analysis/sidecar` isolation harness |
| `test_estimators` | `analysis/estimators` (regime/Hurst/Kalman/VWAP) |
| `test_structure_engine` | `analysis/structure` (ZigZag/fib/cluster) |
| `test_structure_filter` | `analysis/structure_filter` + HTF alignment |
| `test_structure` | `ta/indicators` FVG/Order-Block detectors |
| `test_ta` | `ta/` indicators + registry |
| `test_sl_ladder` | `strategy/rules` SL ratchet |
| `test_execution_guard` | `execution/guard` risk limits |
| `test_trend_filter` | `execution/trend_filter` |
| `test_entry_ttl` | working-order TTL clamp (config) |
| `test_ingest` | `ingest/` contracts + registry |
| `test_infra_helpers` | `timeutil` + `tasks.spawn_bg` |
| `test_ai_effort` | AI effort/config resolution |
| `test_trading_hours` | `trading_hours/` sessions/holidays |
| `test_notifications` | `notifications/config` |
| `test_bus` *(CI)* | `bus` Redis wrapper |
| `test_notifications_dispatch` *(CI)* | `notifications/dispatch` |

Add a test alongside any new pure logic; keep DB/broker imports lazy so the test
stays runnable on a bare box.
