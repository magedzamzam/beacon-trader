FROM python:3.14-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY packages/core /app/packages/core
RUN pip install /app/packages/core
COPY services/api/requirements.txt /tmp/req.txt
RUN pip install -r /tmp/req.txt
COPY services/api/app /app/app
# The commit this image was built from. There is no `.git` and no `git` binary in
# here, so this build arg is the ONLY way a running container can say what code it
# is. Declared LAST on purpose: an ENV that changes every commit invalidates every
# layer beneath it, so putting it above `pip install` (as services/replay does)
# forces a full dependency rebuild on every new SHA. Here a SHA-only change
# rebuilds this one layer. Undeclared args are silently ignored, which is how
# four images went a month without provenance. (#218)
#   GIT_SHA=$(git rev-parse HEAD) docker compose build api
ARG GIT_SHA=""
ENV BEACON_GIT_SHA=$GIT_SHA
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
