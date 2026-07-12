# `docs/` — design notes

Longer-form documentation that doesn't belong in code.

| File | Purpose |
|------|---------|
| `ORDER_TRACKING.md` | How the platform correlates a working order → its resulting position → the closing transaction on Capital.com (working orders and positions have *different* dealIds). Explains the linkage the monitor relies on for fill/close reconciliation. |

Related docs elsewhere:
- Root `README.md` — project overview, architecture, fanout/risk/SL model.
- `INSTALL.md` — deployment runbook.
- `frontend/docs/CONFIGURATION.md` — frontend navigation IA + placeholder catalog.
- Per-folder `README.md` files throughout `packages/` and `services/`.
