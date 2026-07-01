"""Create tables (idempotent). Startup does this automatically; run manually if
you want to provision the schema before first boot."""
import asyncio
from beacon_core.db.base import init_models

if __name__ == "__main__":
    asyncio.run(init_models())
    print("schema ready.")
