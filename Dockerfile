FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

FROM base AS test

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests ./tests
COPY deploy.sh .
COPY scripts ./scripts

CMD ["python", "-m", "pytest", "-q"]

FROM base AS runtime

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data/db /data/tmp \
    && chown -R app:app /data

ENV TMPDIR=/data/tmp

USER app

EXPOSE 8000
VOLUME ["/data/db", "/data/tmp"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
