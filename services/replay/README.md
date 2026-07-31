# services/replay — offline signal replay + backtest harness (#169)

Turns "one hypothesis per frozen live week" into "N hypotheses per afternoon".
It replays the real signal history through the **real** execution engines under
different configs, and reports the result in the same metric shape as a live
weekly.

> **Replay results are hypothesis-generating, not promotion-grade.** The live
> frozen-week A/B/C remains the only thing that promotes a config (CLAUDE.md §2).
> This harness picks which questions are worth a live week; it never answers one.

---

## 1. Isolation from live trading

Non-negotiable, and enforced by construction rather than by convention
(`tests/test_isolation.py` fails CI if any of this stops being true):

| Requirement | How |
|---|---|
| Own service + image | `services/replay`, `profiles: ["research"]` — `docker compose up -d` does **not** start it, so `docker compose stop replay` is trivially a no-op on trading |
| No broker credentials | explicit `environment:` block in compose, **not** `env_file: .env` |
| No order-placing code reachable | import-graph test over the entrypoint, statically (AST) and at runtime (`sys.modules`) |
| Read-only DB | `REPLAY_DATABASE_URL` → a SELECT-only role (`sql/replay_role.sql`). It is **not** given `DATABASE_URL`, and a missing value is a startup error, never a fallback |
| Writes only its own tables | `ReplayBase` is a separate declarative base; `create_all` here has never heard of `trades` |
| No queue participation | nothing imports `bus`, `consume_queue` or `CH_SIGNAL_VALID` |
| Resource-bounded | cpu/memory limits in compose + `nice -n 19`, because `monitor` manages open positions on a tick loop |

Architecturally this is the `beacon_research` half of the #60 ADR: research may
read prod data; prod must never import research.

## 2. Fidelity — the real engines, not a reimplementation

A harness that reimplements the logic tests a different bot. Everything
behavioural is delegated:

```
execution/planner.build_plan            entry model, chase guard, TP geometry
execution/staging.build_staged_legs     tranche partition + deploy levels
execution/staging.decide_tranche        the DECIDE engine
execution/strategy.*                    scope cascade, filtration, cancel_reason
risk/sizing.size_legs / cap_total_risk  lots and the per-signal cap
strategy/rules.evaluate                 the SL ratchet
execution/guard.risk_limit_reason       the caps and the daily-loss breaker
analysis/report.geometry_ab_rollup      the metrics — the same function live uses
```

What lives only here is the part live has no equivalent of: turning a 1m bid/ask
bar into fills and exits, and sequencing them within a bar.

**Within-bar order, fixed and conservative:** staged DECIDE → TTL expiry →
fills → exits (**adverse first**) → MFE + ratchet → `cancel_pending_on_stop`.
The ratchet lands *after* the exits on purpose: a stop a rule would have moved
during a bar does not protect the position retroactively inside it.

### Sided price semantics

Full bid/ask OHLC is what makes exact fills possible. One module (`bars.py`)
owns every predicate:

| direction | enters at | exits at | TP touched | SL touched |
|---|---|---|---|---|
| BUY | ask | bid | `high_bid >= tp` | `low_bid <= sl` |
| SELL | bid | ask | `low_ask <= tp` | `high_ask >= sl` |

Spread is therefore modelled *intrinsically*. `candles.spread_nominal` is never
used: it is the CLOSE spread and disagrees with the open/high/low spreads on the
same bar.

### The conservative choices, all counted and reported

* **Same-bar TP+SL is scored as the STOP.** A 1m bar cannot say which came
  first. `n_same_bar_ambiguous_legs` is a headline field.
* **A LIMIT fills at its level, never better.** Gap improvement is free money
  the harness cannot verify.
* **Slippage is adverse-only** and applies to MARKET/STOP entries and SL exits
  (the fills that cross the spread). LIMIT entries and TP exits are passive.
  Default `0.0` — an explicit operator input, not a number the harness invents.
* **A never-filled entry is excluded from the rollup**, not scored as a
  zero-P&L loss.
* **Still open when the window ends** → marked to market and labelled
  `horizon`, never a win. Counted.
* **`quality != 'ok'` bars are excluded** and the count rides on every result.
* **Equity is constant** (no compounding), so R is not path-dependent and a
  variant that happened to win early cannot beat an identical one that won late.

## 3. Config as data

A run is `{signal_selector, date_range, config_variant}`; a sweep is N variants.
Adding an experiment never means editing the simulator. A variant is shaped like
the live tables it stands in for — `strategies` is `execution_strategies` and is
resolved by the **shipped** scope cascade, so

```json
{"account_id": null, "source_id": null, "exit_policy": {"sl_rules": [ BE@TP1 ]}},
{"account_id": 1,    "source_id": 7,    "exit_policy": {"sl_rules": [ BE@TP2 ]}}
```

is *one* run answering "BE@TP2 for TFXC but BE@TP1 for everyone else". Results
are reported **per-source as well as pooled**, because the correct exit almost
certainly differs by channel (median TP1 ranges 0.15R for TFXC to 1.00R for
Quartz, #182).

See `runs/example-exit-ladder.json`.

## 4. Counterfactual coverage

Every signal is replayed from its **stated entry**, so signals we skipped,
filtered, risk-blocked or never filled are still evaluated — the only way to
score what a filter rejected. Each one ends in exactly one bucket (`taken`, or a
named `not_taken` reason) and every bucket is a row in `replay_results`.

Risk caps and the daily-loss breaker are **simulated**, and a run reports how
many signals each variant's caps blocked. This is why the job atom is
`(variant × account)` — a full portfolio replay — rather than the
`(signal × variant)` the issue proposed: whether signal 412 is taken depends on
what 405 has open and what the account lost that morning. A per-signal atom
would score every blocked signal as taken and overstate every variant. Variants
still share nothing, so the sweep parallelises exactly as intended.

## 5. Overfitting guardrails — enforced, not documented

* **Walk-forward.** `holdout_from` splits the window; the **held-out** result is
  the headline. Without it the report is labelled `in_sample` and says it is not
  reportable as an edge.
* **Search size.** `n_variants_searched` rides on every report along with
  `best_of_n_inflation_sigma` (≈ `sqrt(2 ln N)`) — searching 20 variants buys
  ~2.4σ of pure luck before any skill.
* **Minimum N.** `verdict_withheld` fires below the live N≥30 floor, with a note
  that effective-N ≪ raw-N on correlated same-instrument trades.
* **Regime composition** of the tested window is reported, so robustness cannot
  be claimed beyond the regimes actually tested.
* **Costs modelled**: real spread from `*_ask − *_bid` per bar, slippage, the
  caps, the breaker.

## 6. The validation gate (§5) — run this before believing anything

```bash
docker compose run --rm replay python main.py validate --config runs/live-config.json
```

Replays the **actual live configs** over the **actual signals** and reconciles
against broker truth. Stated thresholds, fixed in code so they cannot be moved
after seeing the number:

* outcome agreement ≥ **0.90** on filled legs
* median |ΔR| ≤ **0.25**
* |mean ΔR| ≤ **0.10** — the **directional** test

The bias test is the important one. Scatter is noise; a harness consistently
**rosier** than live is a blocking defect, because every variant it ranks
inherits the optimism. `validate` exits non-zero on failure.

What it cannot model, and therefore what it is structurally optimistic by:
confirm-404 rejects (#150), orphaned armed STOPs (#161), `fill_price=0` unknown
fills (#159). Those are broker faults with no candle signature. That is a reason
to weight the bias term, not to explain a failure away.

## 7. Phase 2 — the generator seam

```
signal_source:
  historical        -> read `signals` rows            (Phase 1, what-if)
  generator:<name>  -> scan candles, emit signals     (Phase 2)
```

A generator is a pure `(bars, config) -> [GeneratedSignal]` emitting the
existing `ParsedSignal` shape, so planner, sizing, sl_rules, staging and metrics
are byte-identical to the Telegram path. **No generator ships with Phase 1** —
the seam is the deliverable; §8 is explicit that generator search is a
curve-fitting machine, and building one before the harness has passed its own
validation gate would be fitting a model to a simulator nobody has checked.

Path to live is unchanged: a validated generator does **not** go live from a
backtest. It needs a `kind='engine'` source (which does not exist today), a
producer, and shadow forward-R, and only then a weekend config act on one arm.

## 8. Running it

```bash
# 0. once: create the SELECT-only role and put its DSN in .env
psql -f services/replay/sql/replay_role.sql
# REPLAY_DATABASE_URL=postgresql+asyncpg://beacon_replay:...@host:5432/beacon

# 1. do the candles even cover the live signal window?
docker compose run --rm replay python main.py coverage --since 2026-07-05T00:00:00Z

# 2. does the harness reproduce reality? (exits non-zero if not)
docker compose run --rm replay python main.py validate --config runs/live-config.json

# 3. only then, sweep
docker compose run --rm replay python main.py run --config runs/example-exit-ladder.json
```

`check --config` validates a run file offline (no DB). `run --dry-run` prints
the report without writing.

## 9. Results

`replay_runs` (one per execution: selector, window, `n_variants`, `git_sha`,
`config_digest`, `candle_digest`, the full summary) and `replay_results` (one
row per simulated trade **and** per declined signal, with leg outcome labels).
Trade-grained on purpose — trade-level P&L is the only trustworthy basis
(CLAUDE.md §2.5).

Re-running the same config against the same candles reproduces exactly: no RNG,
no wall clock, results assembled by variant name rather than completion order.
If a re-run differs, one of the three digests changed and says which.

## 10. Tests

```bash
PYTHONPATH=packages/core pytest services/replay/tests -q
```

Pure — no DB, no Redis, no broker. Included in the CI suite.
