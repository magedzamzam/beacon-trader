# `db/` — schema & async engine

| File | Purpose |
|------|---------|
| `base.py` | The async SQLAlchemy engine + session factory (`Session()`), the `Base` declarative class, and `init_models()` — a `create_all` plus a few idempotent self-applied migrations (additive `ALTER … IF NOT EXISTS`, the `uq_trades_signal_account` unique index). Safe to call on every startup. |
| `models.py` | The full ORM schema. |

## Key tables
- **Trading ledger:** `Broker`, `Account`, `Source`, `Signal`, `Trade`, `Leg`,
  `Event` (append-only audit), `PositionActivity`, `SymbolMap`.
- **Ingestion/history:** `TelegramMessage`, `SignalClaim`.
- **Config/runtime:** `Setting` (portal-editable settings), `EconEvent`.
- **AI:** `AiAssessment`.
- **Analytics (shadow):** `SignalFeature` (per-signal TA snapshot),
  `SignalAnalytics` (sidecar estimators), and the versioned structure map —
  `MarketStructure`, `StructureLevel`, `MagnetZone`.

## Conventions
- **New TABLES** are created automatically by `create_all` on startup; new
  **COLUMNS** on an existing table need a self-applied `ALTER … IF NOT EXISTS` in
  `init_models` (create_all never adds columns).
- Unique constraints are declared in `__table_args__` (not `index=True` +
  duplicate explicit `Index` — that crashes create_all on a name clash).
- The broker is the source of truth; these tables are a ledger reconciled each
  monitor tick.
