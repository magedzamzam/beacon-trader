"""The notifications router's surface, and its agreement with the API client.

Same contract as `test_analytics_routes` (#175): a route the frontend calls but
the router doesn't serve is a 404 on a live screen that nobody finds out about
until someone opens the page. The delivery log (#181) adds a read-only endpoint
whose only consumer is that page, so pin it here.

Pure: imports the router object and reads api.js as text — no TestClient, no DB.
"""
import re
from pathlib import Path

from app.routers import notifications as N

API_JS = Path(__file__).resolve().parents[3] / "frontend/src/lib/api.js"

EXPECTED = {
    ("GET", "/notifications/catalog"),
    ("GET", "/notifications/fields"),
    ("GET", "/notifications/config"),
    ("PUT", "/notifications/config"),
    ("GET", "/notifications/deliveries"),
    ("POST", "/notifications/test/{channel_id}"),
    ("POST", "/notifications/test-event"),
}


def _served() -> set:
    out = set()
    for r in N.router.routes:
        for method in (r.methods or set()):
            if method not in ("HEAD", "OPTIONS"):
                out.add((method, r.path))
    return out


def test_router_serves_exactly_the_expected_surface():
    assert _served() == EXPECTED


def test_every_notifications_path_the_client_builds_is_served():
    js = API_JS.read_text(encoding="utf-8")
    paths = set(re.findall(r"[\"`](/notifications/[^\"`?]*)", js))
    assert paths, "the client should call the notifications API"
    served = {p for _m, p in _served()}
    for p in paths:
        # `${id}` interpolation -> the router's {param} placeholder
        norm = re.sub(r"\$\{[^}]+\}", "{channel_id}", p).rstrip("/")
        assert norm in served, (p, norm)


def test_deliveries_is_read_only():
    """Telemetry, not a ledger — nothing writes to it through the API."""
    writes = {(m, p) for m, p in _served()
              if p == "/notifications/deliveries" and m != "GET"}
    assert not writes
