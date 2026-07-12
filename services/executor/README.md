# `executor/` — signal → orders

Consumes validated signals off the durable Redis queue and turns each into
risk-sized broker orders. This is the hot path; everything non-essential (TA
capture, analytics) is deferred to the background so it adds no placement
latency.

## `handle_signal` pipeline (per signal)
1. **Idempotency** — short-circuit if the signal is already `executed`
   (re-delivery / retry safe).
2. **Trust gate** — `should_auto_execute` (source enabled + trusted + not
   name-blocklisted); otherwise `blocked`.
3. **Per account** (`_execute_on_account`):
   - fetch equity, quote, current-candle range, FX factor;
   - **trend-alignment filter** (skip/de-size counter-trend, if enabled);
   - `build_plan` → fanout legs; `size_legs` → lots;
   - **risk-limit** enforcement (kill-switch + daily-loss floor always on);
   - optional **AI execution review** (with hard gate);
   - place each leg (MARKET or LIMIT with a broker-enforced `goodTillDate`),
     one `Trade` + N `Leg` rows, guarded by a per-`(signal, account)` unique
     index.
4. Mark the signal `executed`; fire **TA + analytics capture** in the background.

## Background tasks
- **Re-drive sweep** — re-enqueues signals stuck at `validated` with no trade
  (covers the crash-mid-handle window BRPOP can't).
- **Health server** — `/healthz` + heartbeat.

`main.py` is the whole service; the decision logic lives in
`beacon_core.execution` / `risk` / `brokers`. `tests/test_guard.py` covers the
trust guard.
