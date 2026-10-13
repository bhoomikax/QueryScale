"""API endpoint for benchmarking one generated index recommendation."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from backend.config import ROOT_DIR, settings
from backend.jobs.runtime import create_job_store, create_queue, public_job
from backend.jobs.results import aggregate_results as _aggregate_results
from backend.routes.recommendations import read_recommendations
from backend.services.index_manager import validate_candidate


router = APIRouter(prefix="/benchmark", tags=["benchmark"])


def _configured_result_path() -> Path:
    path = Path(settings.benchmark_result_path)
    return path if path.is_absolute() else ROOT_DIR / path


def _find_recommendation(recommendation_id: str) -> dict[str, Any]:
    try:
        recommendations = read_recommendations()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Recommendation data is not available") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Recommendation data is invalid") from exc

    for recommendation in recommendations:
        if recommendation["candidate_id"] == recommendation_id:
            return recommendation
    raise HTTPException(status_code=404, detail=f"Recommendation not found: {recommendation_id}")


def read_benchmark_results() -> list[dict[str, Any]]:
    """Read persisted benchmark measurements for dashboard consumers."""

    result_path = _configured_result_path()
    numeric_fields = {
        "before_avg_ms",
        "before_min_ms",
        "before_max_ms",
        "before_p95_ms",
        "after_avg_ms",
        "after_min_ms",
        "after_max_ms",
        "after_p95_ms",
        "speedup",
        "improvement_percentage",
        "execution_count",
    }
    import csv

    rows: list[dict[str, Any]] = []
    if result_path.exists():
        with result_path.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                parsed: dict[str, Any] = dict(row)
                for field in numeric_fields:
                    parsed[field] = float(row.get(field, "0") or 0)
                parsed["execution_count"] = int(parsed["execution_count"])
                rows.append(parsed)
    for job in create_job_store().list_succeeded(limit=1000):
        for row in (job["result"] or {}).get("results", []):
            rows.append({**row, "job_id": job["job_id"]})
    if not rows:
        raise FileNotFoundError(f"Benchmark result file not found: {result_path}")
    return rows


@router.get("/results")
def get_benchmark_results() -> list[dict[str, Any]]:
    """Return persisted benchmark measurements without starting a new run."""

    try:
        return read_benchmark_results()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/jobs")
def list_benchmark_jobs(limit: int = Query(default=20, ge=1, le=100)) -> list[dict[str, Any]]:
    """List recent persisted benchmark jobs."""

    return [public_job(job) for job in create_job_store().list_recent(limit)]


@router.get("/jobs/{job_id}")
def get_benchmark_job(job_id: str) -> dict[str, Any]:
    """Return status and safe error details for one benchmark job."""

    job = create_job_store().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Benchmark job not found")
    return public_job(job)


@router.get("/jobs/{job_id}/result")
def get_benchmark_job_result(job_id: str) -> dict[str, Any]:
    """Return structured result data after a job succeeds."""

    job = create_job_store().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Benchmark job not found")
    if job["status"] == "failed":
        raise HTTPException(
            status_code=409,
            detail={"status": "failed", "error": job["error_message"]},
        )
    if job["status"] != "succeeded" or job["result"] is None:
        raise HTTPException(
            status_code=409,
            detail={"status": job["status"], "message": "Benchmark result is not ready"},
        )
    return job["result"]


@router.post("/{recommendation_id}", status_code=status.HTTP_202_ACCEPTED)
def benchmark_recommendation(
    recommendation_id: str,
    iterations: int = Query(default=100, ge=1, le=settings.benchmark_job_max_iterations),
) -> dict[str, Any]:
    """Validate a recommendation and enqueue a benchmark without running it inline."""

    recommendation = _find_recommendation(recommendation_id)
    try:
        validated_columns = validate_candidate(
            recommendation["candidate_id"], recommendation["table"], recommendation["columns"]
        )
        score = float(recommendation["score"])
        if not math.isfinite(score):
            raise ValueError("Recommendation score must be finite")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job = create_queue().submit({
        "candidate_id": recommendation["candidate_id"],
        "table": recommendation["table"],
        "columns": list(validated_columns),
        "score": score,
        "priority": str(recommendation["priority"])[:32],
        "iterations": iterations,
    })
    return public_job(job)
