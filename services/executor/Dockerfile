FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY packages/core /app/packages/core
RUN pip install /app/packages/core
COPY services/executor/requirements.txt /tmp/req.txt
RUN if [ -s /tmp/req.txt ]; then pip install -r /tmp/req.txt; fi
COPY services/executor /app
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8080/health')" || exit 1
CMD ["python", "main.py"]
