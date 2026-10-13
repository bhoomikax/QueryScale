"""FastAPI entry point for the QueryScale local application."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.staticfiles import StaticFiles

from backend.routes.benchmark import router as benchmark_router
from backend.routes.queries import router as queries_router
from backend.routes.recommendations import router as recommendations_router
from backend.config import settings
from backend.database import close_connection, get_connection


app = FastAPI(title="QueryScale", version="0.1.0")
app.include_router(benchmark_router, prefix="/api")
app.include_router(queries_router, prefix="/api")
app.include_router(recommendations_router, prefix="/api")
app.mount(
    "/dashboard",
    StaticFiles(directory=Path(__file__).resolve().parents[1] / "dashboard", html=True),
    name="dashboard",
)


@app.get("/health")
def health() -> dict[str, str]:
    """Keep the original process health endpoint for existing consumers."""

    return {"status": "ok"}


@app.get("/health/live")
def health_live() -> dict[str, str]:
    """Report process liveness without checking external dependencies."""

    return {"status": "ok"}


@app.get("/health/ready")
def health_ready() -> dict[str, str]:
    """Report readiness by checking that the configured database accepts a query."""

    connection = None
    cursor = None
    try:
        connection = get_connection()
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database is not ready") from exc
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        close_connection(connection)


def main() -> None:
    import uvicorn

    uvicorn.run("backend.app:app", host=settings.api_host, port=settings.api_port, reload=False)


if __name__ == "__main__":
    main()
