FROM python:3.14-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY packages/core /app/packages/core
RUN pip install /app/packages/core
COPY services/api/requirements.txt /tmp/req.txt
RUN pip install -r /tmp/req.txt
COPY services/api/app /app/app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
