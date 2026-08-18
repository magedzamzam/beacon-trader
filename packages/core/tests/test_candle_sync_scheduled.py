"""The candle store must be kept current by a SERVICE, not by a person (#224).

`public.candles` sat 15 days stale (2026-08-03 -> 2026-08-18) because
`scripts/candle_sync.py` was run by hand once and then never again. Nothing
broke loudly: the #182/#187 excursion ladder silently had no coverage, and no
forward test of the offline generator was possible. The script was not even
committed.

So the thing worth pinning is not that the script exists -- it is that something
RUNS it on a cadence, and that the image it runs from actually contains it.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
COMPOSE = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
SCRIPT = REPO / "scripts" / "candle_sync.py"


def _service_block(name: str) -> str:
    m = re.search(r"^  %s:\s*$" % re.escape(name), COMPOSE, re.M)
    assert m, "no `%s` service in docker-compose.yml" % name
    rest = COMPOSE[m.end():]
    nxt = re.search(r"^  \S+:\s*$", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


def test_the_sync_script_is_committed():
    """It lived untracked on two laptops. That is not a backup."""
    assert SCRIPT.is_file(), "scripts/candle_sync.py is missing"


def test_a_service_runs_it_on_a_cadence():
    block = _service_block("candle-sync")
    assert "candle_sync.py" in block, "the candle-sync service does not run the script"
    m = re.search(r'"--loop",\s*"(\d+)"', block)
    assert m, "candle-sync must pass --loop; a one-shot container exits and never syncs again"
    assert 0 < int(m.group(1)) <= 3600, \
        "a cadence slower than an hour lets the ladder go stale between weeklies"
    assert "restart: unless-stopped" in block, "it must come back after a reboot"


def test_the_image_it_runs_from_contains_the_script():
    """The service reuses the api image; if that image never COPYs scripts/, the
    container starts and dies on ModuleNotFound with nobody watching."""
    block = _service_block("candle-sync")
    m = re.search(r"dockerfile:\s*(\S+)", block)
    assert m, "candle-sync declares no dockerfile"
    df = (REPO / m.group(1)).read_text(encoding="utf-8")
    assert re.search(r"^COPY scripts ", df, re.M), \
        "%s does not COPY scripts/, so /app/scripts/candle_sync.py will not exist" % m.group(1)


def test_the_script_supports_the_loop_flag_it_is_invoked_with():
    src = SCRIPT.read_text(encoding="utf-8")
    assert '"--loop"' in src, "the service passes --loop but the script does not accept it"
    assert "async def sync_once" in src, "the loop needs a single-pass entry point to call"


def test_one_pass_remains_the_default():
    """Every existing manual invocation (`python candle_sync.py --since ...`)
    must still do one pass and exit, or a backfill turns into a daemon."""
    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r'add_argument\("--loop".*?default=(\d+)', src, re.S)
    assert m and int(m.group(1)) == 0, "--loop must default to 0 (one pass, then exit)"


def test_a_failed_pass_does_not_kill_the_loop():
    """A broker hiccup at 03:00 must not leave the store frozen until someone
    notices -- which is exactly how this issue started."""
    src = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"except Exception.*?\n.*?sync pass failed", src), \
        "sync_once() must be wrapped so one bad pass does not exit the loop"
