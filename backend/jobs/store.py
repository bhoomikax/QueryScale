"""Durable local job metadata store backed by SQLite."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SQLiteJobStore:
    """Persist jobs and queue records so API and worker processes can share state."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS benchmark_jobs (
                    job_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    columns_json TEXT NOT NULL,
                    score REAL NOT NULL,
                    priority TEXT NOT NULL,
                    iterations INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
                    phase TEXT NOT NULL DEFAULT 'queued',
                    recovery_required INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    owner TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    result_json TEXT,
                    artifact_ref TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_benchmark_jobs_recent
                    ON benchmark_jobs(created_at DESC);
                CREATE TABLE IF NOT EXISTS local_job_queue (
                    job_id TEXT PRIMARY KEY REFERENCES benchmark_jobs(job_id) ON DELETE CASCADE,
                    enqueued_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        job = dict(row)
        job["columns"] = json.loads(job.pop("columns_json"))
        result_json = job.pop("result_json")
        job["result"] = json.loads(result_json) if result_json else None
        job["recovery_required"] = bool(job["recovery_required"])
        return job

    def enqueue(self, payload: dict[str, Any], job_id: str) -> dict[str, Any]:
        """Create one queued job and its queue record atomically."""

        now = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO benchmark_jobs
                   (job_id, candidate_id, table_name, columns_json, score, priority,
                    iterations, status, phase, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', ?, ?)""",
                (
                    job_id,
                    payload["candidate_id"],
                    payload["table"],
                    json.dumps(payload["columns"]),
                    float(payload["score"]),
                    payload["priority"],
                    int(payload["iterations"]),
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO local_job_queue(job_id, enqueued_at) VALUES (?, ?)",
                (job_id, now),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM benchmark_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return self._decode(row)  # type: ignore[return-value]

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            return self._decode(connection.execute(
                "SELECT * FROM benchmark_jobs WHERE job_id = ?", (job_id,)
            ).fetchone())

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM benchmark_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._decode(row) for row in rows]  # type: ignore[misc]

    def list_succeeded(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM benchmark_jobs WHERE status = 'succeeded' "
                "ORDER BY completed_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._decode(row) for row in rows]  # type: ignore[misc]

    def claim_next(self, owner: str) -> dict[str, Any] | None:
        """Atomically move the oldest queued job to running."""

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT j.* FROM benchmark_jobs AS j
                   JOIN local_job_queue AS q USING (job_id)
                   WHERE j.status = 'queued' ORDER BY q.enqueued_at LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            now = _now()
            connection.execute(
                """UPDATE benchmark_jobs SET status='running', phase='checking', owner=?,
                   attempts=attempts+1, started_at=?, updated_at=?, error_code=NULL,
                   error_message=NULL WHERE job_id=? AND status='queued'""",
                (owner, now, now, row["job_id"]),
            )
            connection.commit()
            claimed = connection.execute(
                "SELECT * FROM benchmark_jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
            return self._decode(claimed)

    def set_phase(self, job_id: str, owner: str, phase: str) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE benchmark_jobs SET phase=?, updated_at=?
                   WHERE job_id=? AND status='running' AND owner=?""",
                (phase, _now(), job_id, owner),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Benchmark job ownership changed unexpectedly")

    def complete(
        self, job_id: str, owner: str, result: dict[str, Any], artifact_ref: str
    ) -> bool:
        now = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE benchmark_jobs SET status='succeeded', phase='complete',
                   updated_at=?, completed_at=?, result_json=?, artifact_ref=?, owner=NULL
                   WHERE job_id=? AND status='running' AND owner=?""",
                (now, now, json.dumps(result), artifact_ref, job_id, owner),
            )
            if cursor.rowcount:
                connection.execute("DELETE FROM local_job_queue WHERE job_id=?", (job_id,))
            connection.commit()
            return cursor.rowcount == 1

    def fail(self, job_id: str, owner: str, code: str, message: str) -> bool:
        now = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE benchmark_jobs SET status='failed', phase='failed', updated_at=?,
                   completed_at=?, error_code=?, error_message=?, owner=NULL
                   WHERE job_id=? AND status='running' AND owner=?""",
                (now, now, code, message, job_id, owner),
            )
            if cursor.rowcount:
                connection.execute("DELETE FROM local_job_queue WHERE job_id=?", (job_id,))
            connection.commit()
            return cursor.rowcount == 1

    def recover_interrupted(self, max_attempts: int = 3) -> int:
        """Requeue jobs left running after the worker process lost its OS lock."""

        now = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE benchmark_jobs SET phase='queued', owner=NULL,
                   recovery_required=CASE WHEN phase='benchmark_started' THEN 1 ELSE recovery_required END,
                   status=CASE WHEN attempts >= ? THEN 'failed' ELSE 'queued' END,
                   updated_at=?, completed_at=CASE WHEN attempts >= ? THEN ? ELSE completed_at END,
                   error_code=CASE WHEN attempts >= ? THEN 'worker_interrupted' ELSE NULL END,
                   error_message=CASE WHEN attempts >= ? THEN 'Worker stopped repeatedly while processing this job.' ELSE NULL END
                   WHERE status='running'""",
                (max_attempts, now, max_attempts, now, max_attempts, max_attempts),
            )
            connection.execute(
                "DELETE FROM local_job_queue WHERE job_id IN "
                "(SELECT job_id FROM benchmark_jobs WHERE status='failed' "
                "AND error_code='worker_interrupted' AND completed_at=?)",
                (now,),
            )
            connection.commit()
            return cursor.rowcount

    def mark_recovery_cleaned(self, job_id: str, owner: str) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE benchmark_jobs SET recovery_required=0, updated_at=?
                   WHERE job_id=? AND status='running' AND owner=?""",
                (_now(), job_id, owner),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Benchmark recovery state changed unexpectedly")
