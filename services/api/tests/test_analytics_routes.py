"""The analytics router's surface, and its agreement with the API client (#175).

#175 deleted six Details views whose underlying data could not inform a decision,
and the routes that existed only to feed them. Two things can go wrong when a page
and its endpoints are removed together, and only one of them is loud:

  * a route outlives its view — dead surface that reads as supported;
  * the frontend keeps calling a path the router no longer serves — which is a
    404 on a live screen, and nobody finds out until someone opens the page.

So this pins the served set and checks every `/analytics/...` path the API client
can build against it. Pure: it imports the router object and reads api.js as text,
no TestClient and no DB.
"""
import re
from pathlib import Path

from app.routers import analytics as A

API_JS = Path(__file__).resolve().parents[3] / "frontend/src/lib/api.js"

# What the router serves, deliberately. Removing a view means removing its route
# in the same commit; adding one means adding it here.
EXPECTED = {
    ("GET", "/analytics/config"),
    ("PUT", "/analytics/config"),
    ("GET", "/analytics/synthesis"),
    ("GET", "/analytics/execution-geometry"),
    ("GET", "/analytics/shadow-strategies"),
    ("GET", "/analytics/turtle-exit"),
    ("GET", "/analytics/structure/config"),
    ("PUT", "/analytics/structure/config"),
    ("GET", "/analytics/structure/map"),
    ("GET", "/analytics/signal/{signal_id}"),
}

# Gone with the views they fed. Listed by name so a re-add is a deliberate act
# with this test in front of it, rather than a quiet reappearance.
REMOVED = {
    "/analytics/correlation",          # channel × regime — regime is constant (#111)
    "/analytics/trend-alignment",      # alignment is a relabelling of direction
    "/analytics/structure",            # FVG/OB outcome cut — chart never read
    "/analytics/structure/outcome",
    "/analytics/structure/recompute",  # the monitor recomputes in-process
    "/analytics/magnets",
}


def _served() -> set:
    out = set()
    for r in A.router.routes:
        for method in (r.methods or set()):
            if method not in ("HEAD", "OPTIONS"):
                out.add((method, r.path))
    return out


def test_router_serves_exactly_the_expected_surface():
    assert _served() == EXPECTED


def test_removed_routes_stay_removed():
    paths = {p for _, p in _served()}
    assert paths & REMOVED == set()


def test_api_client_only_calls_paths_the_router_serves():
    """The 404-on-a-live-screen guard. Every `/analytics/...` literal in api.js is
    reduced to its path (query strings and `${...}` segments dropped) and matched
    against the served set, so deleting a route without its client call — or the
    reverse — fails here instead of in the browser."""
    # Collapse every `${...}` interpolation to a single X FIRST — one of them is
    # `${_perfQs("", range)}`, whose inner quotes would otherwise end the literal
    # mid-path and make this test match nothing at all.
    src = re.sub(r"\$\{[^{}]*\}", "X", API_JS.read_text(encoding="utf-8"))
    served = {p for _, p in _served()}
    # a `{param}` route segment matches any single client segment
    patterns = [re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", p) + "$") for p in served]
    found, unmatched = 0, []
    for raw in re.findall(r"[`\"'](/analytics[^`\"']*)", src):
        found += 1
        path = raw.split("?")[0].rstrip("/")
        # a trailing X is an appended query string (`${_perfQs(...)}`), not a segment
        candidates = {path, path.rstrip("X")}
        if not any(rx.match(c) for c in candidates for rx in patterns):
            unmatched.append(raw)
    assert found >= 5, "found no /analytics calls in api.js — this test is vacuous"
    assert unmatched == [], f"api.js calls unserved analytics paths: {unmatched}"


def test_capture_and_the_surviving_readers_are_untouched():
    """#175's critical line: delete the CHART, keep the CAPTURE. The per-signal
    analytics read and the structure map (which the summary strip's multi-TF bias
    tile still uses) must both survive the cleanup."""
    paths = {p for _, p in _served()}
    assert "/analytics/signal/{signal_id}" in paths
    assert "/analytics/structure/map" in paths
    assert "/analytics/config" in paths          # the capture on/off toggle
