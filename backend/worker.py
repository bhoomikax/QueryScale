"""Background worker for ``JOB_EXECUTION_MODE=queue``.

Run exactly one instance:  ``python -m backend.worker``

The worker claims PENDING jobs from the shared SQLite job store one at a time
and runs the same handlers the API uses in thread mode. An exclusive OS file
lock next to the job database prevents a second worker from starting; the lock
is released automatically if the process exits or crashes. A heartbeat file is
refreshed every few seconds so the API readiness check and the container
health check can tell whether the worker is alive.

The worker never initializes, seeds, or clones databases on startup. It only
does work that a user explicitly submitted through the API.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import IO, Optional

from backend.config import settings
from backend.logging_config import configure_logging
from backend.services.analysis import JOB_HANDLERS
from backend.services.jobs import JobManager


logger = logging.getLogger("backend.worker")
HEARTBEAT_INTERVAL_SECONDS = 5.0


def runtime_dir() -> Path:
    return settings.resolve_path(settings.job_database_path).parent


def heartbeat_path() -> Path:
    return runtime_dir() / "worker.heartbeat"


def heartbeat_age_seconds() -> Optional[float]:
    path = heartbeat_path()
    if not path.exists():
        return None
    return max(0.0, time.time() - path.stat().st_mtime)


class WorkerLock:
    """Exclusive, crash-safe process lock (fcntl on Linux, msvcrt on Windows)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Optional[IO[str]] = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def run_once(manager: JobManager) -> bool:
    """Claim and run one job. Returns False when no job was waiting."""

    job = manager.claim_next()
    if job is None:
        return False
    handler = JOB_HANDLERS.get(job["kind"])
    if handler is None:
        manager.fail(job["job_id"], "unknown_job_kind", f"No handler for job kind {job['kind']!r}")
        return True
    logger.info("Running %s job %s", job["kind"], job["job_id"], extra={"job_id": job["job_id"]})
    finished = manager.run_claimed(job, handler)
    logger.info("Job %s finished with %s", job["job_id"], finished["status"], extra={"job_id": job["job_id"]})
    return True


def _heartbeat_loop(stop: threading.Event) -> None:
    path = heartbeat_path()
    while not stop.is_set():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(time.time()), encoding="utf-8")
        stop.wait(HEARTBEAT_INTERVAL_SECONDS)


def main() -> int:
    configure_logging()
    lock = WorkerLock(runtime_dir() / "worker.lock")
    if not lock.acquire():
        logger.error("Another QueryScale worker holds %s; exiting so jobs are not run twice.", lock.path)
        return 1

    stop = threading.Event()
    for signal_name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, signal_name):
            signal.signal(getattr(signal, signal_name), lambda *_: stop.set())

    heartbeat = threading.Thread(target=_heartbeat_loop, args=(stop,), daemon=True, name="worker-heartbeat")
    heartbeat.start()
    try:
        manager = JobManager(settings.resolve_path(settings.job_database_path), mode="queue", recover_interrupted=False)
        recovered = manager.recover_interrupted(include_pending=False)
        if recovered:
            logger.warning("Marked %s job(s) interrupted by a previous worker as FAILED", recovered)
        logger.info("Worker started (poll every %.1fs, job store %s)", settings.worker_poll_seconds, manager.path)
        while not stop.is_set():
            if not run_once(manager):
                stop.wait(settings.worker_poll_seconds)
    finally:
        stop.set()
        heartbeat.join(timeout=2)
        lock.release()
        logger.info("Worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
