# Market-Structure + Fib Magnet Analytics (#61)

Persistent, versioned multi-timeframe **market structure** + **Fibonacci "magnet
level"** analytics for XAUUSD. This document records exactly what is implemented
today and what is deliberately left for later phases.

> **Hard rule:** everything here is **shadow / measure-before-gate**. It is pure
> observability — nothing in this feature gates, delays, or alters an order. A
> layer only graduates to influencing trading after its edge is measured on
> realized fills.

---

## 1. The two-layer model

**Layer A — the market-wide map (per symbol).** One slow-moving map for XAUUSD
across 8 timeframes (1W…1M), recomputed weekly (or on demand). This is the
"market as a whole" view: the structure state and the confluence "magnet" levels.

**Layer B — the per-signal reference.** Every signal that fires while a map is
active records a lightweight snapshot of *where that signal sits* relative to the
map (per-TF structure, nearest Fib, nearest magnet zone, HTF alignment), tagged
with the map version for point-in-time correctness.

So the analysis is **both** market-wide (Layer A) and per-signal (Layer B).

---

## 2. What is IMPLEMENTED

### 2.1 Pure engine — `beacon_core/analysis/structure.py`
Stdlib-only, fully unit-tested (`tests/test_structure_engine.py`):
- **ATR-scaled ZigZag** swing detection (`zigzag`).
- **HH/HL/LH/LL** labelling (`label_swings`) → **bull/bear/range** classification.
- **Premium/discount** — price position within the active dealing range.
- **Fib ladder** (`fib_ladder`) — retracement **and** extension, both leg
  directions; ratio lists are config.
- **Clustering** (`cluster_levels`) — single-linkage into confluence **magnet
  zones**, scored by Σ(level weight).
- **`feature_contribution()`** — the shared `(name, value, direction, weight,
  confidence)` contract so a future unified signal engine can compose
  structure/regime/TA/bayes uniformly.

### 2.2 Persistence + versioned recompute — `structure_map.py`
- `recompute_symbol()` fetches bars per TF, runs the engine, and writes a **new
  version** (per symbol), superseding the prior — so any signal can be joined to
  the map that was live when it fired (point-in-time).
- `recompute_all()` — the driver: opens its **own isolated session**, resolves an
  adapter + epic per configured symbol, recomputes each. Zero execution-path
  impact.
- `active_map()` — reads the current version (structures + levels + zones) for the
  Layer-B estimator.

### 2.3 Data model — `beacon_core/db/models.py` (auto-created by `create_all`)
| Table | One row per | Key columns |
|-------|-------------|-------------|
| `market_structure` | (symbol, timeframe, version) | `label` (bull/bear/range), `swings`, `premium_discount`, `atr`, `active`, `superseded_at` |
| `structure_levels` | **individual level** | `kind` (fib_retracement/fib_extension/swing_high/swing_low), `ratio`, `price`, `anchor_a/b/c`, `direction`, `weight` |
| `magnet_zones` | confluence cluster | `price_low/high`, `mid`, `score`, `rank`, `n_timeframes`, `ref_atr`, `members` |

Plus the per-signal block in `signal_analytics.analytics.structure_magnet`
(`map_version_id`, `per_tf`, `nearest_zone`, `zones_within_2atr`, `htf_alignment`).

### 2.4 Per-signal estimator (Layer B) — `estimators.py` → `structure_magnet`
Registered in the sidecar suite. Reads the **active** map (does not recompute),
writes the `structure_magnet` block per signal. Runs in the background sidecar —
zero placement latency.

### 2.5 Scheduling & API
- **Weekly recompute** in the monitor (`_maybe_recompute_structure`) — fires on
  the first tick after deploy, then every `recompute_cadence_days`; background,
  own session.
- **Endpoints** (`services/api/app/routers/analytics.py`):
  - `POST /analytics/structure/recompute` — on-demand.
  - `GET  /analytics/structure/map?symbol=XAUUSD` — per-TF structure + zones.
  - `GET  /analytics/structure/config` · `PUT` — the `structure` config.
  - `GET  /analytics/structure/outcome` — Phase-2 measurement (see below).
  - `GET  /analytics/signal/{id}` — includes the `structure_magnet` block.

### 2.6 Config — the `structure` setting (seeded, portal-editable)
`timeframes`, `fib_retracement[]`, `fib_extension[]`, `zigzag_k_by_tf{}`,
`cluster_atr`, `tf_weights{}`, `kind_weights{}`, `recompute_cadence_days`,
`min_bars_by_tf{}`, `symbols[]`, and the nested `filter` block (Phase-3, disabled).

### 2.7 Phase-2 measurement — `report.py` → `structure_magnet_outcome_report`
`GET /analytics/structure/outcome`: win-rate & expectancy cut by **HTF
alignment / magnet proximity / adverse-side**, with Beta-Binomial credible
intervals. This is the measurement Phase-3 gating waits on.

### 2.8 Phase-3 filter scaffolding — `structure_filter.py`
Config `structure.filter` + a pure `decide()` (skip/de-size a signal firing into
an adverse magnet or against HTF structure). **DISABLED and NOT wired into the
executor.** Present so Phase 3 is a config flip + one hook away — no schema
change (the schema already stores `side`/`direction`/`score`/`htf_alignment`).

### 2.9 Frontend (added after the backend)
- **Analytics page → "Market structure & magnet map · XAUUSD"** card: per-TF
  bull/bear/range + premium/discount + ATR + level count, the ranked magnet
  zones (band/mid/score/TFs/members), and a **Recompute** button.
- **Signals page → per-row "Structure & magnets" (layers icon)**: a modal showing
  that signal's `structure_magnet` snapshot — per-TF structure, nearest Fib
  (dist in ATR), nearest magnet zone, and HTF alignment.

---

## 3. What is MISSING / to be implemented

### Phase 2 (measure the edge)
- [ ] **Validate the correlation** on real accumulated data: does magnet
      proximity / HTF alignment actually predict outcome? (needs N to grow; the
      `/structure/outcome` report is the tool, but the *finding* isn't in yet).
- [ ] **Chart overlay** — draw the magnet zones / Fib levels on the price chart
      (the Chart page), not just tables.
- [ ] Feed the result into the multi-factor correlation work (#31) and
      significance display (#18).

### Phase 3 (act on it — only after N≥30 significance)
- [ ] **Enable filtering**: wire `structure_filter.decide()` into the executor
      (one hook) and flip `structure.filter.enabled`. Skip/de-size adverse-magnet
      / counter-HTF signals.
- [ ] **Signal generator**: emit `magnet_setup` candidates (price entering a
      high-score zone from the correct side in an aligned HTF structure). The
      schema already supports this without change.

### Engine / schema gaps (Phase 1 scope-cuts)
- [ ] **`last_event` = BOS / CHoCH** — the column exists but is always `null`;
      break-of-structure / change-of-character detection is not implemented.
- [ ] **Order Block / FVG in the persistent map** — the `kind` enum lists them
      and `kind_weights` includes them, but the recompute currently emits only
      Fib + swing levels. (OB/FVG exist as *per-signal* TA indicators in #59, not
      in Layer A.)
- [ ] **Equal highs/lows** as a distinct kind — currently approximated implicitly
      by clustering two nearby swings.
- [ ] **`anchor_c`** — column exists (for A→B→C retrace anchoring) but is unused;
      only A→B is populated today.
- [ ] **Multi-symbol** — engine + schema are symbol-generic, but config defaults
      to `["XAUUSD"]` and the map card is hard-coded to XAUUSD.

### Operational / correctness
- [ ] **Point-in-time backtest join helper** — the schema supports it
      (`computed_at`/`superseded_at`/`version_id`), but there's no helper that,
      given a signal's `created_at`, returns the exact historical map. The
      estimator records `map_version_id` at capture time (correct for live), but
      offline attribution against superseded versions is manual.
- [ ] **Recompute concurrency guard** — the on-demand endpoint and the weekly
      monitor job could race and both bump the version (low probability; shadow,
      so harmless, but worth a lock).
- [ ] **`beacon_research` package (#60 ADR)** — the engine lives in
      `beacon_core/analysis/` today; the ADR proposes moving it to a dedicated
      `beacon_research` package. Not done.
- [ ] **WEEK-bar availability** — 1W relies on the broker returning `WEEK`
      resolution bars with enough history; unverified on the live feed.

---

## 4. How to operate / verify
1. Deploy (rebuild `api` + `monitor` + `frontend`).
2. `POST /analytics/structure/recompute` (or wait for the monitor's first tick).
   Requires an enabled account + a XAUUSD `SymbolMap`.
3. View: **Analytics → Market structure & magnet map** card, or
   `GET /analytics/structure/map?symbol=XAUUSD`, or SQL on the three tables.
4. Per signal: **Signals → layers icon**, or
   `GET /analytics/signal/{id}` → `.analytics.structure_magnet`.

## 5. Source map
| Concern | File |
|---------|------|
| Pure engine | `packages/core/beacon_core/analysis/structure.py` |
| Persistence / recompute | `packages/core/beacon_core/analysis/structure_map.py` |
| Per-signal estimator | `packages/core/beacon_core/analysis/estimators.py` (`structure_magnet`) |
| Phase-2 report | `packages/core/beacon_core/analysis/report.py` |
| Phase-3 filter (off) | `packages/core/beacon_core/analysis/structure_filter.py` |
| Schema | `packages/core/beacon_core/db/models.py` |
| API | `services/api/app/routers/analytics.py` |
| Scheduling | `services/monitor/main.py` |
| Config seed | `services/api/app/seed.py` |
| Frontend | `frontend/src/pages/Analytics.jsx`, `frontend/src/pages/Signals.jsx` |
| Tests | `packages/core/tests/test_structure_engine.py`, `test_structure_filter.py` |
