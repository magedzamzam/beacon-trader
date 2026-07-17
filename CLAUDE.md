# Beacon Trader — agent guide

Read this before doing anything in this repo. It is the shared process for **all** agents
(analysis agents, fixing agents, PM/quant runs) so we stay consistent.

Beacon turns signals (Telegram / TradingView / manual / API) into risk-sized broker orders,
monitors positions, and moves stops by rule. **It places real orders.** See `README.md` for
architecture, `INSTALL.md` for deployment.

---

## 1. Current context (know this before you touch anything)

- **Phase: DEMO evaluation.** Trading runs on a Capital.com **demo** account. All signal
  sources are deliberately `is_trusted = true` so every channel can be evaluated — *this is
  intentional, not a bug.*
- **We are measuring, not optimising yet.** Per-source samples are still small and correlated
  (all XAUUSD, channels overlap). `min_trades_for_significance = 30`.
- **Account currency is `AEDd`** — the intended AED **demo** tag, and the `AEDDUSD` FX alias is
  load-bearing. **Not a typo; do not "fix" it.**

## 2. Golden rules

1. **Don't change trading logic or live config without being asked.** The executor, monitor,
   planner, `risk/sizing`, and `execution/guard` place and manage real money. Analysis, UI, and
   docs are fair game; the trading path is not — propose it in an issue instead.
2. **Shadow-first.** New analytics (`analysis/`, `ta/`, estimators, structure/magnets) are
   **measure-before-gate**: compute, persist, log — never block or alter a trade until the edge
   is validated. This rule is enforced in `analysis/sidecar.py`; keep it.
3. **Evidence or it's a hypothesis.** Cite `file:line` and/or rows from the daily dump. If you
   can't ground it, label it a hypothesis and say so.
4. **Don't overfit.** Don't act on a per-source verdict below **N ≥ 30** closed trades. The
   feature space dwarfs the sample size — screen univariately, prefer credible-interval *lower*
   bounds, and remember effective-N ≪ raw-N (correlated signals).
5. **Trade-level P&L is trustworthy; leg-level P&L is not** (known cross-attribution bug).
   Use `trades.realized_pl` and leg **outcome labels** (`tp_hit`/`sl_hit`/`breakeven`), never
   `legs.realized_pl`, for analysis.

## 3. Opening an issue — the required process

**Always** use the agent template: `.github/ISSUE_TEMPLATE/agent-detected.yml`
(web: `/issues/new?template=agent-detected.yml`). Never free-form the body.

1. **Dedupe first** — `gh issue list --state open --limit 100` (and check recently closed).
   Related-but-distinct is fine; cross-reference it. Don't refile a closed decision.
2. **Title:** `[Agent] <short, specific title>`
3. **Fields** (the form renders as `### <Label>` + value — mirror exactly when using `gh`):

   | Field | Notes |
   |---|---|
   | Affected Component | `services/executor`, `packages/core/beacon_core/execution/guard.py`, … |
   | Severity | `Critical` / `High` / `Medium` / `Low` |
   | Detection Timestamp (UTC) | ISO-8601, e.g. `2026-07-16T03:00:00Z` |
   | Analysis Summary | problem → root cause (**file:line**) → proposed fix → acceptance criteria |
   | Logs / Evidence | optional; rendered as a shell block |
   | Project Phase | `Initialization` / `Stabilization` / `Enhancement` / `Production` |

4. **Labels** — the template's auto-label only fires via the **web form**. With `gh issue create`
   you must pass it explicitly:
   `--label "status: opened-by-agent"` **plus** the category: `bug`, `enhancement`, `Financial`
   (money/risk/sizing/P&L), `Statistics` (data integrity, metrics, quant), `architecture`, `ux`,
   `epic`, `documentation`.
   Lifecycle labels already exist — **never invent new ones**:
   `status: opened-by-agent | opened-by-user | accepted | in-progress | completed | rejected | duplicate`
5. **Add it to the project** — *Beacon Trader - Issue Lifecycle* (project **#4**), then set
   **Status** and **Phase**:

```bash
ITEM=$(gh project item-add 4 --owner magedzamzam --url <issue-url> --format json | jq -r .id)
# Status = Opened
gh project item-edit --id "$ITEM" --project-id PVT_kwHOA6ZThM4Bdjxy \
  --field-id PVTSSF_lAHOA6ZThM4BdjxyzhYEeNE --single-select-option-id f75ad846
# Phase = Initialization
gh project item-edit --id "$ITEM" --project-id PVT_kwHOA6ZThM4Bdjxy \
  --field-id PVTSSF_lAHOA6ZThM4BdjxyzhYEkHk --single-select-option-id e0c99f34
```

- **Status** `PVTSSF_lAHOA6ZThM4BdjxyzhYEeNE` → Opened `f75ad846` · Accepted `d9498d50` ·
  Work in Progress `47fc9ee4` · Completed `98236657` · Rejected `6d5a325a` · Duplicated `e90bd3a8`
- **Phase** `PVTSSF_lAHOA6ZThM4BdjxyzhYEkHk` → Initialization `e0c99f34` · Stabilization `e7584a39` ·
  Enhancement `3b678c17` · Production `f8813aef`
- Gotchas: `gh project item-list` defaults to **30 items** (`--limit 300` when verifying);
  needs the `project` token scope (`gh auth refresh -s project`).
- The project's **"Auto-add to project"** workflow is already ON, so a new issue lands on the
  board by itself — but it **only adds the item**. It does **not** set **Status** or **Phase**
  (verified 2026-07-17): the filing agent must set **both**. Auto-add can also lag a few
  seconds; `gh project item-add` is **idempotent** (returns the existing item), so just call it
  rather than waiting.

`user-reported.yml` is the maintainer's template — agents use **agent-detected**.

### Status lifecycle — who moves what

| Status | Set by | When |
|---|---|---|
| `Opened` | filing agent | issue created |
| `Accepted` | maintainer | approved for work |
| `Work in Progress` | **fixing agent** | **when the fix is committed/PR'd — this is where an agent STOPS** |
| `Completed` | **maintainer only** | after **he** deploys and verifies it on the demo box |
| `Rejected` / `Duplicated` | either | won't do / dupe |

> **An agent never sets `Completed`.** Finishing the code is not finishing the issue — Beacon
> only proves out once it's deployed and observed. When you commit a fix, set the item to
> **Work in Progress** and say so in the issue; Maged flips it to **Completed** after his own
> deploy + test.

```bash
# after committing a fix (agent's last step on the board):
gh project item-edit --id "$ITEM" --project-id PVT_kwHOA6ZThM4Bdjxy \
  --field-id PVTSSF_lAHOA6ZThM4BdjxyzhYEeNE --single-select-option-id 47fc9ee4   # Work in Progress
```

## 4. Tests & CI

Pure-Python core; no DB/Redis/broker needed.

```bash
pip install -e packages/core          # or: PYTHONPATH=packages/core
pytest packages/core/tests services/executor/tests -q
```

CI (`.github/workflows/tests.yml`) runs this on every push/PR. **Add a test with any change to
sizing, guards, SL rules, the planner, or the analysis layer.**

## 5. Repo map (where things live)

```
packages/core/beacon_core/
  brokers/      broker adapters (capital_com) + FX
  parsing/      signal parser (gold.py)
  ingest/       inbound pipeline -> enqueues CH_SIGNAL_VALID
  execution/    planner.py (fanout/entry model) · guard.py (trust + risk limits)
  risk/         sizing.py (lot/risk math)
  strategy/     rules.py (SL ratchet engine)
  ta/           indicator registry + per-signal capture
  analysis/     bayes · estimators · sidecar (shadow) · structure/magnets · reconcile
  trading_hours/ sessions · news blackout · holidays
  db/models.py  the ledger (broker is source of truth)
services/       api · telegram · collector · executor · monitor
frontend/       React + Vite (Configuration tabs, Positions, Signals, Performance)
```

**Key invariants**
- The executor consumes a **durable queue** (`bus.enqueue` / `consume_queue`), *not* pub/sub.
  Publishing to `CH_SIGNAL_VALID` will be silently dropped.
- `handle_signal` is **idempotent** (skips already-executed signals; one trade per
  signal+account). Re-running a signal must clone it, not replay it.
- SL rules resolve from the **source** (`sources.strategy.sl_rules`); risk config is
  **per-account**.

## 6. Deploy / rebuild gotchas

- `packages/core/beacon_core` is `pip install`ed into **every** Python image → a core change
  means rebuilding **all** python services: `docker compose build api executor monitor telegram`.
- Frontend is a baked Vite build → a JSX change needs `docker compose build frontend`
  (a restart won't do). `redis` never needs rebuilding.
- Schema is `Base.metadata.create_all` on startup: **new tables** appear automatically, **new
  columns on existing tables do not** (needs an explicit ALTER). Never give a column both
  `index=True` and an explicit same-named `Index()` — `create_all` issues CREATE INDEX twice and
  crash-loops every service.

## 7. Data & analysis

- Daily Postgres dumps: `../beacon-data-dump/beacon_YYYYMMDD.sql` (**read-only**, INSERT-format).
  Load them with a **quote-aware statement splitter** — a line-based regex under-loads
  multi-line `raw_text` and silently drops rows.
- Config is snapshotted weekly to `../beacon-claude-ai/trading-bot-pm/config-snapshots/`.
  Changing config mid-week confounds that week's attribution — change at week boundaries.
- PM/quant reports live in `../beacon-claude-ai/`.
