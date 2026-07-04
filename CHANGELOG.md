# Changelog — Alpha Layer

Branch `alpha-layer`. Kept separate from `main` (the live bot) until each phase
is backtested and the operator promotes it.

## Phase 0 — Data Foundation

New service **`collector`** (worker skeleton: health on :8080, heartbeat, async
loop). Captures data the rest of the Alpha Layer depends on.

**New tables** (auto-created by `init_models`/`create_all`):
- `ticks` — top-of-book per symbol (bid, offer, spread, mid, session), on the
  `COLLECT_INTERVAL` cadence.
- `candles` — 1m OHLC (`uq_candle` on symbol+resolution+ts), derived from ticks
  and backfilled from the broker's `get_bars` on boot.
- `cost_profiles` — per symbol × session median / p90 / stddev spread + sample
  count, rebuilt every `COST_PROFILE_INTERVAL` (default 6h) from `ticks` via
  Postgres `percentile_cont`.
- `econ_events` — economic calendar (ts, ccy, impact, title), GMT.
- `crypto_micro` — funding, predicted funding, perp-spot basis, order-book
  imbalance, liquidation proxy (crypto symbols only, once/min).

**New columns** (require `scripts/migrate_001.py` on existing DBs —
`ADD COLUMN IF NOT EXISTS`):
- `signals.provider_ts`, `signals.received_ts`, `signals.published_ts`
- `legs.submitted_ts`, `legs.broker_ack_ts`

**New shared code** (`beacon_core`):
- `marketsessions.session_for(ts)` — GMT-hour → session tag (single source of truth).
- `instruments.asset_class` / `is_crypto` / `binance_symbol`.
- `alpha/calendar.py` — swappable econ-calendar feed (default: ForexFactory
  weekly JSON mirror; override with `ECON_CALENDAR_URL`).
- `alpha/crypto_micro.py` — Binance USDT-perp public REST (funding/basis/OB
  imbalance) + a candle-based `liquidation_proxy` (no public liq feed needed).

**Config** (env): `COLLECT_INTERVAL` (5s), `CANDLE_BACKFILL_BARS` (1000),
`CRYPTO_MICRO_INTERVAL` (60s), `CALENDAR_REFRESH_INTERVAL` (3600s),
`COST_PROFILE_INTERVAL` (21600s), `ECON_CALENDAR_URL`, `BINANCE_FAPI`.

**Guardrails honored**: Decimal throughout; GMT/UTC only; broker session reuse
(one cached adapter per broker); fail-safe (any feed failing logs + is skipped,
the loop never dies); external feeds isolated so they're swappable.

**Tests**: `packages/core/tests/test_alpha_phase0.py` — session boundaries,
asset classification, Binance mapping, and the liquidation proxy. 8/8 pass.

**Deploy**: `docker compose build collector && docker compose up -d collector`,
then run the column migration once:
`docker compose run --rm api python -m scripts.migrate_001`.

### Acceptance (operator-verified, needs a live run)
After ~24h: `ticks` full-session coverage <1% gaps; `cost_profiles` populated;
`econ_events` shows next week's high-impact events; `crypto_micro` populated for
at least BTCUSD (requires a BTCUSD SymbolMap).

## Phases 1–5 (planned)
1. Backtest engine (event-driven, imports real `build_plan`/`size_legs`, cost
   model, deflated Sharpe).
2. Regime service (Kaufman ER, ATR expansion, session, blackout, cost gate) +
   executor `regime_policy` gate.
3. Scorecard & allocator (Bayesian shrinkage, alpha-decay, markout,
   `sources.risk_multiplier`).
4. Three internal strategy modules as `kind="internal"` Sources (strategist
   service), shipped OFF.
5. Walk-forward validation + `/reports/alpha`. Promotion gates & kill switches.
