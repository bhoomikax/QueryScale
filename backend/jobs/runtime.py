"""Construction helpers for the configured local job store and adapters."""

from __future__ import annotations

from pathlib import Path

from backend.config import ROOT_DIR, settings
from backend.jobs.artifacts import LocalArtifactStore
from backend.jobs.queue import LocalSQLiteJobQueue
from backend.jobs.store import SQLiteJobStore


def _configured_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT_DIR / path


def create_job_store() -> SQLiteJobStore:
    return SQLiteJobStore(_configured_path(settings.job_database_path))


def create_queue(store: SQLiteJobStore | None = None) -> LocalSQLiteJobQueue:
    return LocalSQLiteJobQueue(store or create_job_store())


def create_artifact_store() -> LocalArtifactStore:
    return LocalArtifactStore(_configured_path(settings.job_artifact_directory))


def public_job(job: dict) -> dict:
    """Remove worker internals and expose stable API fields only."""

    return {
        "job_id": job["job_id"],
        "candidate_id": job["candidate_id"],
        "table": job["table_name"],
        "columns": job["columns"],
        "iterations": job["iterations"],
        "status": job["status"],
        "attempts": job["attempts"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "started_at": job["started_at"],
        "completed_at": job["completed_at"],
        "error_code": job["error_code"],
        "error": job["error_message"],
        "artifact_ref": job["artifact_ref"],
        "result_url": f"/api/benchmark/jobs/{job['job_id']}/result"
        if job["status"] == "succeeded"
        else None,
    }
