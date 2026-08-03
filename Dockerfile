FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY app ./app

FROM base AS test

COPY requirements-dev.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements-dev.lock
COPY tests ./tests
COPY docs/current/openapi.json ./docs/current/openapi.json
COPY deploy.sh .
COPY compose.yaml .
COPY scripts ./scripts

CMD ["python", "-m", "pytest", "-q"]

FROM base AS runtime

RUN pip uninstall --yes pip setuptools wheel \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /data/db /data/tmp \
    && chown -R app:app /data

ENV TMPDIR=/data/tmp

USER app

EXPOSE 8000
VOLUME ["/data/db", "/data/tmp"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
