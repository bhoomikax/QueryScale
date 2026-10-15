"""In-process background jobs with persistent state (SQLite, standard library only).

States: PENDING -> RUNNING -> COMPLETED | FAILED.

Execution modes (``JOB_EXECUTION_MODE``):

* ``thread`` (default): the API process runs jobs in a small thread pool.
* ``queue``: the API only records PENDING jobs. A single worker process
  (``python -m backend.worker``) claims them one at a time from the same SQLite
  file and runs the same handlers. Docker Compose uses this so long benchmarks
  never run inside the API container.

Duplicate protection: for each job kind only one job may be active. Submitting
the same kind with the same dedupe key while it is active returns the existing
job (idempotent). A different key raises ``JobConflict``. Jobs left PENDING or
RUNNING by a previous process are marked FAILED on startup.

SQLite needs a local filesystem shared by the API and worker (one host, one
Docker volume). For several hosts, use a networked queue such as SQS and keep
this table, or an equivalent, as the shared job status record.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from backend.config import settings


logger = logging.getLogger(__name__)

PENDING, RUNNING, COMPLETED, FAILED = "PENDING", "RUNNING", "COMPLETED", "FAILED"
ACTIVE_STATES = (PENDING, RUNNING)
EXECUTION_MODES = ("thread", "queue")
_INTERRUPTED = {"code": "interrupted", "message": "The process stopped before the job finished."}


class JobConflict(RuntimeError):
    def __init__(self, active_job: Dict[str, Any]) -> None:
        super().__init__(f"A {active_job['kind']} job is already {active_job['status'].lower()}: {active_job['job_id']}")
        self.active_job = active_job


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class JobManager:
    def __init__(
        self,
        database_path: str | Path,
        max_workers: int = 2,
        run_inline: bool = False,
        mode: str | None = None,
        recover_interrupted: bool | None = None,
    ) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.mode = "thread" if run_inline else (mode or settings.job_execution_mode).strip().lower()
        if self.mode not in EXECUTION_MODES:
            raise ValueError(f"JOB_EXECUTION_MODE must be one of {EXECUTION_MODES}, got {self.mode!r}")
        self.run_inline = run_inline
        self._lock = threading.Lock()
        threaded = self.mode == "thread" and not run_inline
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="queryscale-job") if threaded else None
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS jobs ("
                "job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, dedupe_key TEXT NOT NULL, status TEXT NOT NULL, "
                "params TEXT, result TEXT, error TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_kind_status ON jobs (kind, status)")
        # Only the process that executes jobs may declare unfinished jobs interrupted. In queue
        # mode the API must not, or it would fail jobs the worker is still running.
        should_recover = recover_interrupted if recover_interrupted is not None else self.mode == "thread"
        if should_recover:
            self.recover_interrupted(include_pending=self.mode == "thread")

    def recover_interrupted(self, include_pending: bool = False) -> int:
        """Mark RUNNING (and optionally PENDING) jobs left by a stopped executor as FAILED."""

        states = ACTIVE_STATES if include_pending else (RUNNING,)
        placeholders = ", ".join("?" for _ in states)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET status = ?, finished_at = ?, error = ? WHERE status IN ({placeholders})",
                (FAILED, _now(), json.dumps(_INTERRUPTED), *states),
            )
            return cursor.rowcount

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Autocommit connection that is always closed (sqlite3's own context manager does not close)."""

        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        job = dict(row)
        for key in ("params", "result", "error"):
            job[key] = json.loads(job[key]) if job.get(key) else None
        return job

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._to_dict(row) if row else None

    def list(self, kind: str | None = None, limit: int = 20) -> List[Dict[str, Any]]:
        query = "SELECT * FROM jobs"
        params: List[Any] = []
        if kind:
            query += " WHERE kind = ?"
            params.append(kind)
        query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            return [self._to_dict(row) for row in connection.execute(query, params).fetchall()]

    def latest(self, kind: str) -> Optional[Dict[str, Any]]:
        jobs = self.list(kind, limit=1)
        return jobs[0] if jobs else None

    def _update(self, job_id: str, **fields: Any) -> None:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [json.dumps(v, default=str) if key in {"params", "result", "error"} and v is not None else v for key, v in fields.items()]
        with self._connect() as connection:
            connection.execute(f"UPDATE jobs SET {assignments} WHERE job_id = ?", (*values, job_id))

    def submit(self, kind: str, dedupe_key: str, params: Dict[str, Any], func: Callable[[Dict[str, Any]], Dict[str, Any]]) -> tuple[Dict[str, Any], bool]:
        """Create and start a job. Returns ``(job, created)``; created is False for an idempotent repeat."""

        with self._lock:
            with self._connect() as connection:
                # BEGIN IMMEDIATE makes check-then-insert atomic across processes as well as threads.
                connection.execute("BEGIN IMMEDIATE")
                active = connection.execute(
                    "SELECT * FROM jobs WHERE kind = ? AND status IN (?, ?) ORDER BY created_at DESC LIMIT 1",
                    (kind, *ACTIVE_STATES),
                ).fetchone()
                if active is not None:
                    connection.execute("ROLLBACK")
                    job = self._to_dict(active)
                    if job["dedupe_key"] == dedupe_key:
                        return job, False
                    raise JobConflict(job)
                job_id = f"{kind}-{uuid.uuid4().hex[:12]}"
                connection.execute(
                    "INSERT INTO jobs (job_id, kind, dedupe_key, status, params, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (job_id, kind, dedupe_key, PENDING, json.dumps(params, default=str), _now()),
                )
                connection.execute("COMMIT")
        if self.mode == "queue":
            return self.get(job_id), True
        if self.run_inline:
            self._run(job_id, func, params)
        else:
            self._executor.submit(self._run, job_id, func, params)
        return self.get(job_id), True

    def claim_next(self) -> Optional[Dict[str, Any]]:
        """Atomically move the oldest PENDING job to RUNNING and return it (queue-mode worker)."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at, rowid LIMIT 1", (PENDING,)
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                return None
            connection.execute(
                "UPDATE jobs SET status = ?, started_at = ? WHERE job_id = ? AND status = ?",
                (RUNNING, _now(), row["job_id"], PENDING),
            )
            connection.execute("COMMIT")
        return self.get(row["job_id"])

    def run_claimed(self, job: Dict[str, Any], func: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        """Execute a job returned by ``claim_next`` and return its final record."""

        self._run(job["job_id"], func, job.get("params") or {}, mark_running=False)
        return self.get(job["job_id"])

    def fail(self, job_id: str, code: str, message: str) -> None:
        self._update(job_id, status=FAILED, finished_at=_now(), error={"code": code, "message": message})

    def _run(
        self,
        job_id: str,
        func: Callable[[Dict[str, Any]], Dict[str, Any]],
        params: Dict[str, Any],
        mark_running: bool = True,
    ) -> None:
        if mark_running:
            self._update(job_id, status=RUNNING, started_at=_now())
        try:
            result = func(params)
            self._update(job_id, status=COMPLETED, finished_at=_now(), result=result)
        except Exception as exc:
            code = getattr(exc, "code", None) or ("invalid_request" if isinstance(exc, (ValueError, FileNotFoundError)) else "job_failed")
            logger.exception("Job %s failed", job_id)
            self._update(job_id, status=FAILED, finished_at=_now(), error={"code": code, "message": str(exc)})


_manager: Optional[JobManager] = None
_manager_lock = threading.Lock()


def get_job_manager() -> JobManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = JobManager(settings.resolve_path(settings.job_database_path))
        return _manager


def set_job_manager(manager: Optional[JobManager]) -> None:
    """Replace the global manager (used by tests)."""

    global _manager
    with _manager_lock:
        _manager = manager
