"""Benchmark endpoints: run a recommendation on the test database and read measured results.

``POST /api/benchmark/{id}`` keeps its synchronous behavior by default. With
``background=true`` it returns ``202`` and a job that can be polled at
``/api/jobs/{job_id}``. Only one benchmark runs at a time; a second request for
the same candidate returns the active job, a request for a different candidate
returns ``409``.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from backend.config import ROOT_DIR, settings
from backend.routes.recommendations import read_recommendations
from backend.services.benchmark import run_benchmark_detailed
from backend.services.index_manager import BenchmarkError
from backend.services.jobs import JobConflict, get_job_manager
from backend.services.ml.dataset import load_benchmark_history
from backend.services.stats import optional_float, parse_bool


router = APIRouter(prefix="/benchmark", tags=["benchmark"])

_STATUS_FOR_CODE = {
    "benchmark_in_progress": 409,
    "test_database_missing": 503,
    "connection_error": 503,
    "permission_denied": 403,
    "authentication_failed": 503,
    "query_timeout": 504,
    "index_already_exists": 409,
}


def _configured_result_path() -> Path:
    path = Path(settings.benchmark_result_path)
    return path if path.is_absolute() else ROOT_DIR / path


def _find_recommendation(recommendation_id: str) -> dict[str, Any]:
    try:
        recommendations = read_recommendations()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    for recommendation in recommendations:
        if recommendation["candidate_id"] == recommendation_id:
            return recommendation
    raise HTTPException(status_code=404, detail=f"Recommendation not found: {recommendation_id}")


def _aggregate_results(results: list[dict[str, object]]) -> dict[str, float | int]:
    """Execution-weighted mean latency across query groups (legacy dashboard summary)."""

    if not results:
        raise ValueError("Benchmark produced no query results")
    execution_count = sum(int(row["execution_count"]) for row in results)
    if execution_count <= 0:
        raise ValueError("Benchmark results contain no measured executions")
    before_ms = sum(float(row["before_avg_ms"]) * int(row["execution_count"]) for row in results) / execution_count
    after_ms = sum(float(row["after_avg_ms"]) * int(row["execution_count"]) for row in results) / execution_count
    return {
        "before_ms": round(before_ms, 3),
        "after_ms": round(after_ms, 3),
        "speedup": round(before_ms / after_ms, 3) if after_ms else 0.0,
        "improvement_percentage": round((before_ms - after_ms) / before_ms * 100, 3) if before_ms else 0.0,
        "execution_count": execution_count,
    }


_NUMERIC_FIELDS = {
    "before_avg_ms", "before_min_ms", "before_max_ms", "before_p95_ms",
    "after_avg_ms", "after_min_ms", "after_max_ms", "after_p95_ms",
    "speedup", "improvement_percentage", "execution_count",
    "before_median_ms", "after_median_ms", "median_speedup", "median_latency_reduction_pct",
    "before_rows_examined", "after_rows_examined",
}
_BOOLEAN_FIELDS = {"uses_candidate_index", "supporting", "affected"}


def read_benchmark_results() -> list[dict[str, Any]]:
    """Read persisted benchmark measurements. Missing values are returned as null, not zero."""

    result_path = _configured_result_path()
    if not result_path.exists():
        raise FileNotFoundError(f"Benchmark result file not found: {result_path}")

    rows: list[dict[str, Any]] = []
    with result_path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            parsed: dict[str, Any] = dict(row)
            for field in _NUMERIC_FIELDS:
                if field in row:
                    parsed[field] = optional_float(row.get(field))
            for field in _BOOLEAN_FIELDS:
                if row.get(field):
                    parsed[field] = parse_bool(row[field])
                elif field in row:
                    parsed[field] = None
            parsed["execution_count"] = int(parsed.get("execution_count") or 0)
            parsed["measurement_type"] = "measured"
            rows.append(parsed)
    return rows


@router.get("/results")
def get_benchmark_results() -> list[dict[str, Any]]:
    """Return per-query measurements from the latest successful benchmark run."""

    try:
        return read_benchmark_results()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _history_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": record.get("run_id"),
        "status": record.get("status"),
        "candidate_id": record.get("candidate_id"),
        "table": record.get("table"),
        "columns": record.get("columns"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "server_version": record.get("server_version"),
        "dataset_signature": record.get("dataset_signature"),
        "workload": record.get("workload"),
        "summary": record.get("summary"),
        "index_size_bytes": record.get("index_size_bytes"),
        "index_size_source": record.get("index_size_source"),
        "write_overhead": {
            key: value for key, value in (record.get("write_overhead") or {}).items() if key.endswith("_pct")
        } or None,
        "cleanup": record.get("cleanup"),
        "error": record.get("error"),
    }


@router.get("/history")
def get_benchmark_history(
    limit: int = Query(default=50, ge=1, le=500),
    candidate_id: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    """Return benchmark runs (completed and failed), newest first."""

    records = load_benchmark_history(settings.resolve_path(settings.benchmark_history_path))
    if candidate_id:
        records = [r for r in records if r.get("candidate_id") == candidate_id]
    return [_history_summary(r) for r in reversed(records)][:limit]


@router.get("/history/{run_id}")
def get_benchmark_run(run_id: str) -> dict[str, Any]:
    """Return one full benchmark run, including per-query rows and EXPLAIN plans."""

    for record in load_benchmark_history(settings.resolve_path(settings.benchmark_history_path)):
        if record.get("run_id") == run_id:
            return record
    raise HTTPException(status_code=404, detail=f"Benchmark run not found: {run_id}")


def _job_params(recommendation: dict[str, Any], iterations: int, repeats: int) -> dict[str, Any]:
    return {
        "candidate_id": recommendation["candidate_id"],
        "table": recommendation["table"],
        "columns": ",".join(recommendation["columns"]),
        "iterations": iterations,
        "repeats": repeats,
        "supporting_query_ids": recommendation.get("supporting_query_ids") or None,
    }


@router.post("/{recommendation_id}")
def benchmark_recommendation(
    recommendation_id: str,
    iterations: int = Query(default=100, ge=1, le=10000),
    repeats: int = Query(default=1, ge=1, le=10),
    background: bool = Query(default=False),
) -> Any:
    """Apply a recommendation to the test DB only and return measured before/after results."""

    recommendation = _find_recommendation(recommendation_id)
    params = _job_params(recommendation, iterations, repeats)

    if background:
        from backend.services.analysis import run_benchmark_job

        dedupe_key = f"{params['candidate_id']}:{iterations}:{repeats}"
        try:
            job, created = get_job_manager().submit("benchmark", dedupe_key, params, run_benchmark_job)
        except JobConflict as exc:
            raise HTTPException(status_code=409, detail={"message": str(exc), "active_job": exc.active_job}) from exc
        return JSONResponse(status_code=202, content={**job, "created": created, "status_url": f"/api/jobs/{job['job_id']}"})

    try:
        record = run_benchmark_detailed(
            candidate_id=params["candidate_id"],
            table=params["table"],
            columns=params["columns"],
            iterations=iterations,
            output_path=_configured_result_path(),
            repeats=repeats,
            supporting_query_ids=params["supporting_query_ids"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BenchmarkError as exc:
        raise HTTPException(
            status_code=_STATUS_FOR_CODE.get(exc.code, 503),
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    results = record["queries"]
    return {
        "candidate_id": recommendation["candidate_id"],
        "table": recommendation["table"],
        "columns": recommendation["columns"],
        "score": recommendation["score"],
        "priority": recommendation["priority"],
        **_aggregate_results(results),
        "run_id": record["run_id"],
        "workload_summary": record["summary"],
        "index_size_bytes": record.get("index_size_bytes"),
        "write_overhead": record.get("write_overhead"),
        "cleanup": record.get("cleanup"),
        "results": results,
    }
