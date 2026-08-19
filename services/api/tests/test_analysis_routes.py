"""The analysis router's surface, and its agreement with the API client.

Same contract as `test_analytics_routes` (#175): a path the frontend calls but the
router doesn't serve is a 404 on a live screen that nobody finds out about until
someone opens the page. The R-ladder card (#182) is the newest consumer, and this
box has no npm — so a test is the only thing standing between a client/router
drift and the operator.

Pure: imports the router object and reads api.js as text — no TestClient, no DB.
"""
import re
from pathlib import Path

from app.routers import analysis as A

API_JS = Path(__file__).resolve().parents[3] / "frontend/src/lib/api.js"

EXPECTED = {
    ("GET", "/analysis/bayes"),
    ("GET", "/analysis/bayes/score/{signal_id}"),
    ("GET", "/analysis/bayes-gate/config"),
    ("PUT", "/analysis/bayes-gate/config"),
    ("GET", "/analysis/bayes-gate/report"),
    ("GET", "/analysis/excursion"),
    ("POST", "/analysis/excursion/recompute"),
    ("GET", "/analysis/stop-geometry"),          # #189, shadow
    ("GET", "/analysis/shadow_engine"),          # #239, Lever 5 measurement
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


def test_every_analysis_path_the_client_builds_is_served():
    js = API_JS.read_text(encoding="utf-8")
    paths = set(re.findall(r"[\"`](/analysis/[^\"`?]*)", js))
    assert paths, "the client should call the analysis API"
    served = {p for _m, p in _served()}
    for p in paths:
        norm = re.sub(r"\$\{[^}]+\}", "{signal_id}", p).rstrip("/")
        assert norm in served, (p, norm)


def test_the_excursion_recompute_is_the_only_write():
    """The ladder is shadow telemetry. Reading it must never mutate anything, and
    the one POST is the deliberate offline replay — not something a page polls."""
    writes = {(m, p) for m, p in _served()
              if p.startswith("/analysis/excursion") and m != "GET"}
    assert writes == {("POST", "/analysis/excursion/recompute")}
