"""FastAPI entry point for the QueryScale application."""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.config import settings
from backend.logging_config import configure_logging
from backend.routes.analysis import router as analysis_router
from backend.routes.benchmark import router as benchmark_router
from backend.routes.ml import router as ml_router
from backend.routes.queries import router as queries_router
from backend.routes.recommendations import candidates_router
from backend.routes.recommendations import router as recommendations_router


configure_logging()
logger = logging.getLogger("backend.app")

app = FastAPI(title="QueryScale", version="0.2.0")
app.include_router(benchmark_router, prefix="/api")
app.include_router(queries_router, prefix="/api")
app.include_router(recommendations_router, prefix="/api")
app.include_router(candidates_router, prefix="/api")
app.include_router(analysis_router, prefix="/api")
app.include_router(ml_router, prefix="/api")
app.mount(
    "/dashboard",
    StaticFiles(directory=Path(__file__).resolve().parents[1] / "dashboard", html=True),
    name="dashboard",
)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    if request.url.path.startswith("/api"):
        logger.info(
            "%s %s -> %s", request.method, request.url.path, response.status_code,
            extra={
                "request_id": request_id,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
    return response


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    error_id = uuid.uuid4().hex[:12]
    logger.exception("Unhandled error %s on %s", error_id, request.url.path)
    return JSONResponse(status_code=500, content={"detail": {"code": "internal_error", "message": "Internal server error", "error_id": error_id}})


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is ready to receive requests."""

    return {"status": "ok"}


@app.get("/health/live")
def health_live() -> dict[str, str]:
    """Liveness alias used by the ECS container health check and the AWS scripts."""

    return {"status": "ok"}


@app.get("/health/ready")
def readiness(check_db: bool = Query(default=False)) -> dict[str, Any]:
    """Readiness: artifact availability, model status, and (optionally) database connectivity."""

    from backend.services.ml.predictor import load_ranker

    artifacts = {
        name: settings.resolve_path(path).exists()
        for name, path in {
            "query_log": settings.query_log_path,
            "recommendations": settings.recommendation_path,
            "benchmark_results": settings.benchmark_result_path,
        }.items()
    }
    ranker = load_ranker()
    report: dict[str, Any] = {
        "status": "ok",
        "artifacts": artifacts,
        "ranking_method": "ml" if ranker.usable else "rule",
        "model": {"available": ranker.available, "usable": ranker.usable, "reason": ranker.reason},
        "artifact_store": settings.artifact_store,
        "jobs": {"execution_mode": settings.job_execution_mode},
    }
    if settings.job_execution_mode == "queue":
        from backend.worker import heartbeat_age_seconds

        age = heartbeat_age_seconds()
        report["jobs"]["worker_heartbeat_age_seconds"] = None if age is None else round(age, 1)
        if age is None or age > 30:
            report["jobs"]["worker"] = "not running: queued jobs will stay PENDING"
            report["status"] = "degraded"
        else:
            report["jobs"]["worker"] = "running"
    if check_db:
        from backend.database import close_connection, get_connection

        connection = None
        try:
            connection = get_connection()
            report["database"] = "reachable"
        except RuntimeError:
            report["database"] = "unreachable"
            report["status"] = "degraded"
        finally:
            close_connection(connection)
    return report


def main() -> None:
    import uvicorn

    uvicorn.run("backend.app:app", host=settings.api_host, port=settings.api_port, reload=False)


if __name__ == "__main__":
    main()
