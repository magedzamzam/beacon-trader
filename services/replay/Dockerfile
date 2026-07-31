# services/replay — offline research harness (#169).
#
# ISOLATION IS THE POINT. Compared to every other service image:
#   * NO broker credentials. The container is started with an explicit, minimal
#     `environment:` block in docker-compose (NOT `env_file: .env`), so the
#     Capital.com variables are not present to be read even by accident.
#   * NO health endpoint and NO long-running process — this is a batch job,
#     invoked with `docker compose run --rm replay ...`. A service that is not
#     running cannot interfere with one that is.
#   * beacon_core is installed read-only for its PURE engines (planner, sizing,
#     staging, sl_rules, strategy resolution). The import-graph test in
#     tests/test_isolation.py fails the build's test stage if anything that can
#     place or cancel an order becomes reachable from this entrypoint.
FROM python:3.14-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY packages/core /app/packages/core
RUN pip install /app/packages/core
COPY services/replay/requirements.txt /tmp/req.txt
RUN if [ -s /tmp/req.txt ]; then pip install -r /tmp/req.txt; fi
COPY services/replay /app
# Nice-level the process: a wide sweep is CPU-bound and must never win a
# scheduling contest against `monitor`, which manages OPEN POSITIONS on a tick
# loop. It is an ENTRYPOINT, not part of CMD, so it still applies when the
# command is overridden — which it always is:
#   docker compose run --rm replay python main.py run --config runs/…json
# Compose adds the hard cpu/memory ceiling on top of this.
ENTRYPOINT ["nice", "-n", "19"]
CMD ["python", "main.py", "--help"]
