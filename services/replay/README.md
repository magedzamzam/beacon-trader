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
| Writes only its own tables | `ReplayBase` is a separate declarative base; `create_all` here has never heard of `trades`. Its tables live in a separate `replay` schema, and it holds no `CREATE` in `public` at all |
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
  zero-P&L loss — and, since #185, the closest its side ever came to its level
  is recorded, so "the simulator under-fills" is a distribution rather than a
  suspicion (`caveats.under_fill`).
* **A ratchet takes effect from the NEXT bar** by default, so a stop does not
  protect the position retroactively. That is conservative about the stop and
  *optimistic* about the outcome — it skips breakevens live took. `same_bar`
  (#185) is the alternative; the gate decides which is kept.
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
* **Return is a PERIOD return**, not annualised. `returns.return_pct` is simple
  return on starting equity over whatever window the run covered; rank on R
  (scale-free), quote the percentage. With a holdout, `returns` is the held-out
  figure and `returns_pooled` is the whole window — named apart so an in-sample
  percentage cannot be quoted as the result.

### Session windows (#81)

A variant carrying a `trading_hours: {"sessions": [...]}` block models both live
session mechanisms, through the shipped `trading_hours.sessions` functions: the
risk **multiplier** (the London/NY overlap de-size) and `ctx['sessions']`, which
`session_in` filter rules match against. Session and filter factors multiply and
apply to the *risk config*, so the per-signal cap and the min-lot check both see
the de-sized plan — exactly the order the executor uses.

Without the block neither is modelled, which is the default: turning it on by
guessing `DEFAULT_SESSIONS` would silently change every run config written
before it shipped. `scaffold` reads the real setting, so the validation baseline
gets it automatically, and every result states `sessions_modelled` so a variant
that modelled them is never silently compared against one that did not.

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

### A pass is not a clean bill of health (#185)

The first real gate run **passed** — agreement 0.9216, median |ΔR| 0.063, mean ΔR
+0.060 — and **76% of its 72 disagreements sat in two cells**, pulling in
opposite directions:

| sim | live | n | what it is |
|---|---|---|---|
| `expired` | filled (any outcome) | 34 | **defect A** — the simulator under-fills entries |
| `tp_hit` | `breakeven` | 21 | **defect B** — the ratchet takes effect a bar late |

`disagreement_bias: "balanced"` is a balance of COUNTS between two unrelated
mechanisms. They cancel by accident, not by construction: the ratio depends on
the signal mix, so a sweep over one channel, session or regime will not enjoy the
same offset — **and per-channel verdicts are exactly where that bites**.

**Defect A is now diagnosed rather than guessed.** Three candidate causes, three
different fixes, and the counts alone cannot separate them:

- **the candle feed** — a spread printing wider than Capital.com's was, so the
  simulated ask never reaches a level the real ask did;
- **the TTL or the replay window**;
- **within-bar ordering** — TTL expiry runs before fills, so an order whose TTL
  lapsed on a bar it *would* have filled in is lost.

Every resting order now records the closest its fillable side ever came to its
level, and the third candidate is counted outright. `validate` reports an
`under_fill` block (miss distribution, how many missed by ≤ 0.5 points, how many
were retired on a fillable bar, what live went on to do with them) and a
**verdict pointing at the most likely cause**. The same distribution rides on
every run under `caveats.under_fill`, because a sweep inherits whatever the
under-fill is doing. Note these legs are **invisible in mean ΔR** — an entry the
simulator never took is a trade it never scored — while still changing which
signals each variant is ranked on.

> The `under_fill` verdict is a **pointer, not a conclusion**. If it reads
> feed-shaped, the thing that settles it is still the outstanding
> feed-provenance diff from #169: median and max |Δ| on **high/low** against
> Capital.com bars over an overlapping day, since high/low decide fill/TP/SL.
> That needs the broker, so it is an operator step this harness cannot do.

**Defect B is now selectable rather than assumed.** `ratchet_timing` on a
variant:

- `next_bar` (**default, unchanged**) — the stop moves after this bar's exits
  resolve, protecting the position only from the next bar. Conservative about
  the stop, optimistic about the outcome: it skips breakevens live actually took.
- `same_bar` — after the ratchet, the freshly-moved stops are re-tested against
  **this** bar's adverse extreme, i.e. a monitor that ratcheted mid-minute and
  was taken out by the retrace inside the same minute.

It is a second exit pass, not a reordering: the leg whose own TP armed the rule
still takes that TP, exactly as the broker's resting TP order does live.

Neither is obviously right, so **the gate decides**:

```bash
docker compose run --rm --no-deps replay python main.py validate \
  --config runs/live-config.json --ratchet-timing next_bar   # today's model
docker compose run --rm --no-deps replay python main.py validate \
  --config runs/live-config.json --ratchet-timing same_bar   # the alternative
```

Keep whichever brings **mean ΔR** closer to zero while agreement holds, and
document the loser's residual. The flag overrides the variants themselves, not
just `defaults` — a baseline that already stated a timing would otherwise win and
you would be comparing a run against itself. The setting is recorded on every
result (`settings.ratchet_timing`), printed on the gate output, and changes the
variant digest: two runs under different exit models are not comparable and must
not share a fingerprint.

## 7. Phase 2 — generated signals (#184)

```
signal_source:
  historical        -> read `signals` rows            (Phase 1, what-if)
  generator:rules   -> scan candles, emit signals     (Phase 2, backtest)
```

A generator is a pure `(bars, config) -> [GeneratedSignal]` emitting the
existing `ParsedSignal` shape, so planner, sizing, sl_rules, staging and metrics
are byte-identical to the Telegram path.

**One generator ships, and only one: `generator:rules`.** Registering a Python
function per idea — `generator:macd`, `generator:fvg`, `generator:order_block` —
is the shape this deliberately does not have: it would repeat #167's mistake on
the generation side, where gating on an indicator meant hand-writing an
evaluator *and* hand-plumbing a ctx key, which does not scale to a 45-entry
registry. **A new strategy is JSON, not a deploy.**

The condition grammar is the **same one `entry_filters` uses**
(`packages/core/beacon_core/execution/strategy.py`), now composable:

```json
{"all": [
  {"type": "indicator", "id": "macd", "timeframe": "15m", "field": "cross",
   "op": "eq", "value": "bull"},
  {"type": "indicator", "id": "rsi", "timeframe": "15m", "field": "value",
   "op": "lt", "value": 70},
  {"any": [{"type": "indicator", "id": "fvg", "timeframe": "15m",
            "field": "present", "op": "is_true"},
           {"type": "indicator", "id": "order_block", "timeframe": "15m",
            "field": "present", "op": "is_true"}]},
  {"not": {"type": "session_in", "sessions": ["New York"]}}
]}
```

`all` / `any` / `not` wrap the existing leaf types, so adding a leaf benefits
filtration and generation at once. Evaluation is **three-valued**: a leaf whose
input is missing is UNKNOWN, and UNKNOWN never fires — including through `not`,
which is the whole reason it is three-valued. Two-valued logic would make
`{"not": X}` true whenever X could not be computed, i.e. a generator emitting on
the *absence* of evidence.

What is genuinely new is `entry` / `sl` / `tps` — that is what makes a condition
a **signal**, and what R is measured against. `entry`: `close` | `level` |
`offset_atr`. `sl`: `atr_mult` | `points` | `level`. `tps[]`: `r_mult` |
`atr_mult` | `points` | `level`. A geometry that cannot be priced from the bar,
or that fails `planner.validate_signal`, is **dropped and counted** — never
completed with a guessed level.

`cooldown_bars` and `max_signals_per_day` are **not optional** (and default to
non-zero). A condition true for 50 consecutive bars emits 50 signals, each
opening a position, and `max_open_risk_per_symbol` then decides the strategy —
you would be measuring the risk caps, not the indicator. Both, and everything
the generator suppressed or dropped, ride on the run header under `generator`.

No look-ahead: a trigger bucket's condition is evaluated at that bucket's
**close** and sees only buckets that had fully closed by then
(`ContextBuilder.closed_bars` owns that boundary; the generator does not
re-implement it), and the signal is timestamped at the same instant. Generated
signals carry **negative** `signal_id`s so a join against the trading `signals`
table cannot silently match the wrong rows.

See `runs/example-generator-rules.json`, and `check --config` for the offline
resolve — it prints the indicator instances the condition will compute, which is
the only place an unknown indicator id becomes visible (a condition naming one is
UNKNOWN on every bar and emits nothing, silently, by design).

### This is a screening step, not a route to live

Unchanged, and it is the part most likely to be forgotten:

- **It is not actionable until the §5 validation gate has passed.** Fitting a
  strategy to a simulator nobody has verified compounds two unknowns.
- **Held-out is the only reportable result.** In-sample generator output is a
  description of the past, not an edge.
- **`n_variants_searched` counts the whole grid**, not the arms shown — a 4×5×3
  sweep is 60, and best-of-60 carries ~2.9σ of pure luck.
- **N ≥ 30 per variant**, with effective-N ≪ raw-N (correlated same-instrument
  trades), and the window's **regime composition** stated: ~7 months of 2026 gold
  is one or two regimes.
- A validated generator does **not** go live from a backtest. It needs the
  Lever-5 chain — a `kind='engine'` source (which does not exist today;
  `sources.kind ∈ {telegram, tradingview, manual, api}`), a producer, and shadow
  forward-R — and only then a weekend config act on **one** arm.

One useful asymmetry: Telegram signals only exist from **2026-07-05** (~4 weeks),
whereas candles run from **2026-01-01**. Generated signals are therefore the only
route to a materially longer sample today — which is also the first time the
walk-forward split has had room to mean anything.

## 8. Running it

```bash
# 0. once, as the DATABASE OWNER (no superuser needed): create the SELECT-only
#    role + its schema, then put the DSN in .env. Edit the placeholders first
#    (password, database name, and `beacon_app` -> whoever OWNS the trading
#    tables). The file ends with a VERIFY section and a decisive "try to write
#    and be refused" test — run both before deploying.
psql -f services/replay/sql/replay_role.sql
# REPLAY_DATABASE_URL=postgresql+asyncpg://beacon_replay:...@host:5432/beacon

# 1. the code is COPYed into the image, so ANY change needs a rebuild
#    (CLAUDE.md 6). A stale image is the one failure mode that looks like a
#    code bug and is not.
git pull && docker compose build replay

# 2. create the two replay.* tables. Two seconds, and it is the ONLY write this
#    service ever makes - so it is the cheap proof that the grant works.
docker compose run --rm --no-deps replay python main.py init

# 3. do the candles even cover the live signal window?
docker compose run --rm --no-deps replay python main.py coverage --since 2026-07-05T00:00:00Z

# 4. generate the validation baseline FROM the live tables. Hand-transcribing
#    execution_strategies + account_source_risk + risk_limits into JSON is how
#    you end up validating the transcription instead of the simulator.
#    --equity is required: it lives at the broker, not in the ledger.
docker compose run --rm --no-deps replay python main.py scaffold --equity 10000
#    ...then read the `_needs_review` list it prints, and fix what it names.

# 5. does the harness reproduce reality? (exits non-zero if not)
docker compose run --rm --no-deps replay python main.py validate --config runs/live-config.json

# 6. only then, sweep
docker compose run --rm --no-deps replay python main.py run --config runs/example-exit-ladder.json

# 7. ...and only after 5 passes, generated signals (#184). `check` first: it
#    resolves the condition offline and prints the indicator instances it will
#    compute, which is the only place an unknown indicator id is visible.
docker compose run --rm --no-deps replay python main.py check --config runs/example-generator-rules.json
docker compose run --rm --no-deps replay python main.py run   --config runs/example-generator-rules.json
```

`run` performs the same init before it simulates anything, so a grant problem
fails in seconds rather than after a completed sweep.

**Always `--no-deps`.** The service declares no `depends_on`, so there is
nothing for compose to start — but `docker compose run` is a command that CAN
start other containers, and this one must never be the reason a trading service
changes state. `--no-deps` removes the question. Likewise never pass
`--remove-orphans`: that acts on the whole project and would delete containers
that are not in `docker-compose.yml` but may be doing something.

`check --config` validates a run file offline (no DB). `run --dry-run` prints
the report without writing.

### There is no UI *on this service* — and no way to start a run from one

Deliberately, and unchanged. **This service has no API endpoints.** It is a batch
job behind a `research` profile, and giving the trading API a route that triggers
it would make a 400k-bar scan startable from a browser, competing with `monitor`
for CPU on the box that manages open positions.

What #183 added is a **read path on the API that already exists**: three GETs
(`/replay/runs`, `/replay/runs/{id}`, `/replay/runs/{id}/results`) over the
`replay.*` TABLES, plus the portal's *Backtest (Replay)* page. Reading a table is
not importing a module — the router holds a local read-model and never imports
`harness.*`, which `tests/test_isolation.py` and
`services/api/tests/test_replay_routes.py` assert from both sides. There is no
POST, no client call, and no button anywhere that starts, queues or schedules a
run. It needs a one-off `GRANT SELECT … IN SCHEMA replay` to the API role (see
the bottom of `sql/replay_role.sql`); until that is run the routes report
`available: false` and print the SQL.

Results are equally readable with SQL, as `beacon_replay` — which can read every
trading table plus the `replay.*` results, and write none of them:

```sql
-- did a variant beat the baseline, and on how many trades?
SELECT variant,
       count(*) FILTER (WHERE ever_filled)                      AS n,
       round(avg(r_multiple) FILTER (WHERE ever_filled), 3)     AS avg_R,
       count(*) FILTER (WHERE NOT taken)                        AS declined
  FROM replay.replay_results WHERE run_id = 1 GROUP BY 1 ORDER BY 3 DESC;

-- why were the declined ones declined?
SELECT variant, not_taken_reason, count(*)
  FROM replay.replay_results WHERE run_id = 1 AND NOT taken GROUP BY 1,2;

-- the full report, guardrails included
SELECT jsonb_pretty(summary::jsonb) FROM replay.replay_runs WHERE id = 1;
```

`run` also prints the ranking to stdout, so a sweep is readable without touching
the database at all.

## 9. Results

`replay.replay_runs` (one per execution: selector, window, `n_variants`,
`git_sha`, `config_digest`, `candle_digest`, the full summary) and
`replay.replay_results` (one row per simulated trade **and** per declined
signal, with leg outcome labels). Both in a separate `replay` schema — so
`create_all` needs no privilege in `public`, and there is never a follow-up
grant to remember.

`beacon_replay` is also the right connection for *analysing* a run: it can read
every trading table plus the `replay.*` results, and can write none of them.
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
