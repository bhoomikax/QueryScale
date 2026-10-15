"""Workload analysis jobs and generic job status endpoints."""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.services.analysis import run_analysis
from backend.services.jobs import JobConflict, get_job_manager


router = APIRouter(tags=["analysis"])


class AnalysisRequest(BaseModel):
    metadata_source: Literal["auto", "mysql", "snapshot", "schema"] = "auto"
    use_ml: bool = True


@router.post("/analysis", status_code=202)
def start_analysis(request: Optional[AnalysisRequest] = None) -> JSONResponse:
    """Start workload analysis in the background. Repeating an identical active request returns the same job."""

    params = (request or AnalysisRequest()).model_dump()
    dedupe_key = f"{params['metadata_source']}:{params['use_ml']}"
    try:
        job, created = get_job_manager().submit("analysis", dedupe_key, params, run_analysis)
    except JobConflict as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "active_job": exc.active_job}) from exc
    return JSONResponse(status_code=202, content={**job, "created": created, "status_url": f"/api/jobs/{job['job_id']}"})


@router.get("/analysis/status")
def analysis_status() -> dict[str, Any]:
    """Return the latest analysis job, or NEVER_RUN when none exists."""

    job = get_job_manager().latest("analysis")
    return job or {"kind": "analysis", "status": "NEVER_RUN"}


@router.get("/jobs")
def list_jobs(
    kind: Optional[Literal["analysis", "benchmark", "training"]] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict[str, Any]]:
    return get_job_manager().list(kind, limit)


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = get_job_manager().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return job
