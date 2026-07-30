"""Make `services/api` importable from pytest (#165).

There is no `services/__init__.py` or `services/api/__init__.py`, and the app's
modules use package-relative imports (`from ..deps import get_db`), so the app
package only resolves with `services/api` itself on the path. Putting it there
here means tests import exactly what uvicorn does — `from app.routers import
strategies` — with no shim in between.

`packages/core` comes from `pip install -e packages/core` (CI) or PYTHONPATH, as
for every other suite in this repo.
"""
import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]      # services/api
REPO_ROOT = API_ROOT.parents[1]                     # repo root

if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))
