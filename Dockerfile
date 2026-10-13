FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/runtime/job_artifacts \
    && chown -R appuser:appuser /app/runtime

COPY backend ./backend
COPY scripts ./scripts
COPY dashboard ./dashboard
COPY --chown=10001:10001 data ./data

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('API_PORT', '8000') + '/health/live', timeout=3)"

CMD ["sh", "-c", "uvicorn backend.app:app --host ${API_HOST:-0.0.0.0} --port ${API_PORT:-8000}"]
