"""Local benchmark worker entry point. Run with ``py -m backend.jobs.worker``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import uuid
from typing import Callable

from backend.config import settings
from backend.jobs.artifacts import LocalArtifactStore
from backend.jobs.lock import LocalProcessLock
from backend.jobs.queue import LocalSQLiteJobQueue
from backend.jobs.results import aggregate_results
from backend.jobs.runtime import create_artifact_store, create_job_store, create_queue
from backend.jobs.store import SQLiteJobStore
from backend.services.benchmark import run_benchmark
from backend.services.index_manager import candidate_index_exists, remove_candidate_index


LOGGER = logging.getLogger("queryscale.worker")


class LocalJobWorker:
    """Consume jobs serially under a kernel-managed cross-process file lock."""

    def __init__(
        self,
        store: SQLiteJobStore,
        queue: LocalSQLiteJobQueue,
        artifacts: LocalArtifactStore,
        runner: Callable[..., list[dict[str, object]]] = run_benchmark,
        timeout_seconds: int = 900,
        max_attempts: int = 3,
    ):
        self.store = store
        self.queue = queue
        self.artifacts = artifacts
        self.runner = runner
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.owner = f"{os.getpid()}:{uuid.uuid4()}"
        lock_path = store.database_path.with_name(store.database_path.name + ".worker.lock")
        self.lock = LocalProcessLock(lock_path)

    @staticmethod
    def _force_timeout_exit(job_id: str) -> None:
        sys.stderr.write(f"QueryScale benchmark job {job_id} exceeded its time limit; worker exiting.\n")
        sys.stderr.flush()
        os._exit(124)

    def run_once(self) -> bool:
        if not self.lock.acquire():
            return False
        try:
            self.store.recover_interrupted(self.max_attempts)
            job = self.queue.claim_next(self.owner)
            if job is None:
                return False
            self._process(job)
            return True
        finally:
            self.lock.release()

    def _process(self, job: dict) -> None:
        job_id = job["job_id"]
        candidate_id = job["candidate_id"]
        table = job["table_name"]
        columns = job["columns"]
        phase_started = False
        result = None
        failure: tuple[str, str] | None = None
        timer = threading.Timer(self.timeout_seconds, self._force_timeout_exit, args=(job_id,))
        timer.daemon = True
        timer.start()

        try:
            if job["recovery_required"]:
                remove_candidate_index(candidate_id, table, columns)
                self.store.mark_recovery_cleaned(job_id, self.owner)

            if candidate_index_exists(candidate_id, table, columns):
                self.store.fail(
                    job_id,
                    self.owner,
                    "candidate_index_exists",
                    "This candidate index already exists in the test database. Rebuild the disposable test database before submitting another benchmark.",
                )
                return

            # Persist this marker before run_benchmark can create the index. A restarted
            # worker can then remove only this job's deterministic candidate index.
            self.store.set_phase(job_id, self.owner, "benchmark_started")
            phase_started = True
            output_path = self.artifacts.result_path(job_id)
            output_path.unlink(missing_ok=True)
            results = self.runner(
                candidate_id=candidate_id,
                table=table,
                columns=",".join(columns),
                iterations=job["iterations"],
                output_path=output_path,
            )
            result = {
                "candidate_id": candidate_id,
                "table": table,
                "columns": columns,
                "score": job["score"],
                "priority": job["priority"],
                **aggregate_results(results),
                "results": results,
            }
            if not output_path.exists():
                raise RuntimeError("Benchmark report was not written")
        except ValueError as exc:
            LOGGER.info("Benchmark job %s rejected: %s", job_id, exc)
            failure = ("invalid_benchmark", str(exc))
        except Exception:
            LOGGER.exception("Benchmark job %s failed", job_id)
            failure = (
                "benchmark_failed",
                "Benchmark failed. Check worker logs and confirm the disposable test database is ready.",
            )
        finally:
            # On ordinary benchmark failure, remove an index only after this job
            # recorded that the candidate was absent from the test database.
            if phase_started:
                try:
                    remove_candidate_index(candidate_id, table, columns)
                except Exception:
                    LOGGER.exception("Candidate-index cleanup failed for job %s", job_id)
                    failure = (
                        "test_database_cleanup_failed",
                        "Benchmark cleanup failed. Rebuild the disposable test database before retrying.",
                    )
            timer.cancel()

        if failure is not None:
            self.store.fail(job_id, self.owner, *failure)
        elif result is not None:
            self.store.complete(job_id, self.owner, result, self.artifacts.reference(job_id))
        else:
            self.store.fail(
                job_id,
                self.owner,
                "benchmark_failed",
                "Benchmark did not produce a result.",
            )

    def run_forever(self, poll_seconds: float = 1.0) -> None:
        LOGGER.info("QueryScale local benchmark worker started")
        while True:
            if not self.run_once():
                time.sleep(poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Consume QueryScale local benchmark jobs.")
    parser.add_argument("--once", action="store_true", help="Process at most one queued job and exit.")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    store = create_job_store()
    worker = LocalJobWorker(
        store,
        create_queue(store),
        create_artifact_store(),
        timeout_seconds=settings.benchmark_job_timeout_seconds,
        max_attempts=settings.benchmark_job_max_attempts,
    )
    if args.once:
        worker.run_once()
        return 0
    try:
        worker.run_forever(max(args.poll_seconds, 0.1))
    except KeyboardInterrupt:
        LOGGER.info("QueryScale local benchmark worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
