# `scripts/` — operator utilities

Standalone helper scripts (not part of any running service).

| File | Purpose |
|------|---------|
| `init_db.py` | Create the schema (idempotent). Startup already calls `init_models()` automatically, so this is only for provisioning the DB **before** first boot: `python scripts/init_db.py`. |

Run scripts with the shared library importable (e.g. inside a service container,
where `beacon_core` is installed).
