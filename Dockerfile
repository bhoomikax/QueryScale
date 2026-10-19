# QueryScale API and worker image. One image, two commands (see compose.yaml and
# infra/terraform/ecs_services.tf). No credentials, .env files, model artifacts, or
# benchmark history are copied in. The committed data/*.csv files are included so a
# cloud task can serve recommendations before any analysis job has run; Compose
# bind-mounts ./data over them locally.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data /app/models /app/runtime \
    && chown -R appuser:appuser /app/data /app/models /app/runtime

COPY backend ./backend
COPY scripts ./scripts
COPY database ./database
COPY dashboard ./dashboard
COPY --chown=10001:10001 data ./data
# Amazon RDS certificate authorities so DB_TLS_VERIFY=true works against RDS.
# Source: https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
# Unused locally; point DB_TLS_CA at this path in deployed environments.
COPY infra/certs/rds-global-bundle.pem ./certs/rds-global-bundle.pem

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('API_PORT', '8000') + '/health/live', timeout=3)"

CMD ["sh", "-c", "uvicorn backend.app:app --host ${API_HOST:-0.0.0.0} --port ${API_PORT:-8000}"]
