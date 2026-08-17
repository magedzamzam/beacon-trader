"""Every buildable image records the commit it was built from (#218).

An undeclared Docker build arg is not an error — it is silently dropped. So
`GIT_SHA=$(git rev-parse HEAD) docker compose build monitor` looked correct for a
month while `BEACON_GIT_SHA` stayed empty in four of five images, and the only
way to tell which code was running after a deploy was to import a symbol that had
not existed before and see whether it resolved.

The wiring is two halves in two files, which is exactly the shape that rots: the
`args:` in `docker-compose.yml` and the `ARG`/`ENV` pair in the Dockerfile. Either
half alone is silent. So this pins BOTH, for every service with a `build:`
stanza, and a new service is covered by default rather than by remembering.

Pure text checks — no docker, no yaml dependency, runs on a bare box.
"""
from pathlib import Path

import re

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPO_ROOT / "docker-compose.yml"

# Exclusions are a reviewed act with a reason next to them, the convention
# `test_every_table_is_guarded` set. Both of these are deliberate, not oversights.
#
# `frontend` is a static Vite bundle, not a python service: nothing in it reads
# an env var at runtime, so `BEACON_GIT_SHA` would be inert. Its provenance
# belongs in the built asset, which is a different job (#218 follow-on).
PROVENANCE_EXEMPT = {"frontend"}
#
# `services/replay/Dockerfile` declares the ARG above its `pip install`, so every
# new commit rebuilds the research image's dependency layers. That is
# pre-existing and costs nothing on the trading path — #218 scoped itself to the
# four python images and deliberately did not touch it. Remove this entry when
# that Dockerfile is reordered; the assertion below is what will confirm it.
LAYER_ORDER_EXEMPT = {"services/replay/Dockerfile"}


def _build_services() -> dict:
    """{service: dockerfile-path relative to the repo root} for every service
    with a `build:` stanza, EXCLUDING the reviewed exemptions above.

    The path is `context` joined with `dockerfile`, because compose resolves it
    that way — `frontend` builds `Dockerfile` against a `./frontend` context, and
    reading the two keys independently points at the wrong file.

    Hand-rolled rather than via pyyaml: the test suite is dependency-free on
    purpose, and the shape being read here is two keys deep."""
    services, current, in_services = {}, None, False
    context, dockerfile, in_build = None, None, False

    def _flush():
        if current and dockerfile and current not in PROVENANCE_EXEMPT:
            base = (context or ".").lstrip("./")
            services[current] = "%s/%s" % (base, dockerfile) if base else dockerfile

    for raw in COMPOSE.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if re.match(r"^services:\s*$", raw):
            in_services = True
            continue
        if in_services and re.match(r"^\S", raw):        # left column ends services:
            break
        m = re.match(r"^  (\S+):\s*$", raw)              # a service name
        if m:
            _flush()
            current, context, dockerfile, in_build = m.group(1), None, None, False
            continue
        if re.match(r"^    build:\s*$", raw):
            in_build = True
            continue
        if in_build:
            m = re.match(r"^      context:\s*(\S+)\s*$", raw)
            if m:
                context = m.group(1)
            m = re.match(r"^      dockerfile:\s*(\S+)\s*$", raw)
            if m:
                dockerfile = m.group(1)
    _flush()
    return services


def _service_block(name: str) -> str:
    """The raw text of one service's block, up to the next service."""
    text = COMPOSE.read_text(encoding="utf-8")
    start = re.search(r"^  %s:\s*$" % re.escape(name), text, re.M)
    assert start, "service %s not found in docker-compose.yml" % name
    rest = text[start.end():]
    nxt = re.search(r"^  \S+:\s*$", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


def test_compose_has_build_services():
    """Guard the guard: if the parse breaks, every assertion below passes vacuously."""
    services = _build_services()
    assert set(services) >= {"api", "executor", "monitor", "telegram"}, services


@pytest.mark.parametrize("service", sorted(_build_services()))
def test_service_passes_git_sha_build_arg(service):
    block = _service_block(service)
    assert "GIT_SHA:" in block, (
        "%s has a build: stanza but passes no GIT_SHA arg, so its image cannot "
        "say what commit produced it" % service)


@pytest.mark.parametrize("service,dockerfile", sorted(_build_services().items()))
def test_dockerfile_declares_and_exports_git_sha(service, dockerfile):
    path = REPO_ROOT / dockerfile
    assert path.is_file(), "%s references a missing %s" % (service, dockerfile)
    text = path.read_text(encoding="utf-8")
    assert re.search(r"^ARG GIT_SHA=", text, re.M), (
        "%s: compose passes GIT_SHA but the Dockerfile never declares it — an "
        "undeclared build arg is silently dropped, not an error" % dockerfile)
    assert re.search(r"^ENV BEACON_GIT_SHA=\$GIT_SHA\s*$", text, re.M), (
        "%s: ARG without the ENV means the value never reaches the running "
        "container" % dockerfile)


@pytest.mark.parametrize("service,dockerfile", sorted(_build_services().items()))
def test_git_sha_is_declared_after_the_dependency_layers(service, dockerfile):
    """A per-commit ENV above `pip install` rebuilds dependencies every time.

    Docker invalidates every layer below a changed one, so an ENV that moves with
    each commit belongs BELOW the expensive installs. `services/replay/Dockerfile`
    is the counter-example this is guarding against being copied."""
    if dockerfile in LAYER_ORDER_EXEMPT:
        pytest.skip("%s is a reviewed exemption — see LAYER_ORDER_EXEMPT" % dockerfile)
    lines = (REPO_ROOT / dockerfile).read_text(encoding="utf-8").splitlines()
    arg_at = next(i for i, l in enumerate(lines) if l.startswith("ARG GIT_SHA="))
    installs = [i for i, l in enumerate(lines) if re.match(r"^RUN .*pip install", l)]
    if not installs:
        pytest.skip("%s installs nothing worth caching" % dockerfile)
    assert arg_at > max(installs), (
        "%s declares ARG GIT_SHA at line %d, above a `pip install` at line %d — "
        "every new commit would rebuild the dependency layers. Move it below the "
        "installs and the source COPY." % (dockerfile, arg_at + 1, max(installs) + 1))
