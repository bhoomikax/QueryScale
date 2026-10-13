import tempfile
import unittest
import multiprocessing
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from backend.jobs.artifacts import LocalArtifactStore
from backend.jobs.lock import LocalProcessLock
from backend.jobs.queue import LocalSQLiteJobQueue
from backend.jobs.runtime import public_job
from backend.jobs.store import SQLiteJobStore
from backend.jobs.worker import LocalJobWorker
from backend.routes import benchmark as benchmark_routes


def payload(iterations=8):
    return {
        "candidate_id": "IDX_TRANSACTIONS_ACCOUNT_ID",
        "table": "Transactions",
        "columns": ["account_id"],
        "score": 88.25,
        "priority": "high",
        "iterations": iterations,
    }


def successful_runner(**kwargs):
    Path(kwargs["output_path"]).write_text("benchmark report", encoding="utf-8")
    return [{
        "candidate_id": kwargs["candidate_id"],
        "query_id": "Q001",
        "before_avg_ms": 10.0,
        "before_min_ms": 9.0,
        "before_max_ms": 11.0,
        "before_p95_ms": 10.9,
        "after_avg_ms": 5.0,
        "after_min_ms": 4.0,
        "after_max_ms": 6.0,
        "after_p95_ms": 5.9,
        "speedup": 2.0,
        "improvement_percentage": 50.0,
        "execution_count": 2,
    }]


def _attempt_lock_in_child(path: str, result_queue) -> None:
    lock = LocalProcessLock(path)
    acquired = lock.acquire()
    result_queue.put(acquired)
    if acquired:
        lock.release()


def _acquire_lock_and_exit(path: str, result_queue) -> None:
    lock = LocalProcessLock(path)
    result_queue.put(lock.acquire())


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "jobs.sqlite3"
        self.store = SQLiteJobStore(self.database_path)
        self.queue = LocalSQLiteJobQueue(self.store)

    def test_job_state_and_result_survive_store_reinitialization(self):
        job = self.queue.submit(payload())
        self.assertEqual(job["status"], "queued")
        claimed = self.queue.claim_next("worker-a")
        self.assertEqual(claimed["status"], "running")
        self.store.set_phase(job["job_id"], "worker-a", "benchmark_started")
        result = {"candidate_id": job["candidate_id"], "results": []}
        self.assertTrue(self.store.complete(job["job_id"], "worker-a", result, "local-ref"))

        reopened = SQLiteJobStore(self.database_path)
        persisted = reopened.get(job["job_id"])
        self.assertEqual(persisted["status"], "succeeded")
        self.assertEqual(persisted["result"], result)
        self.assertEqual(persisted["artifact_ref"], "local-ref")
        self.assertIsNone(LocalSQLiteJobQueue(reopened).claim_next("worker-b"))

    def test_queue_claim_is_atomic_and_duplicate_claim_does_not_rerun_job(self):
        job = self.queue.submit(payload())
        first = self.queue.claim_next("worker-a")
        second = self.queue.claim_next("worker-b")
        self.assertEqual(first["job_id"], job["job_id"])
        self.assertIsNone(second)
        self.assertEqual(self.store.get(job["job_id"])["attempts"], 1)

    def test_failed_transition_removes_pending_queue_record(self):
        job = self.queue.submit(payload())
        self.queue.claim_next("worker-a")
        self.assertTrue(self.store.fail(job["job_id"], "worker-a", "worker_error", "safe error"))
        self.assertEqual(self.store.get(job["job_id"])["status"], "failed")
        self.assertIsNone(self.queue.claim_next("worker-b"))

    def test_recovery_marks_started_jobs_for_candidate_index_cleanup(self):
        job = self.queue.submit(payload())
        self.queue.claim_next("dead-worker")
        self.store.set_phase(job["job_id"], "dead-worker", "benchmark_started")
        self.assertEqual(self.store.recover_interrupted(max_attempts=3), 1)

        recovered = self.store.get(job["job_id"])
        self.assertEqual(recovered["status"], "queued")
        self.assertTrue(recovered["recovery_required"])

    def test_repeated_worker_crashes_fail_job_after_attempt_limit(self):
        job = self.queue.submit(payload())
        self.queue.claim_next("worker-1")
        self.assertEqual(self.store.recover_interrupted(max_attempts=1), 1)
        recovered = self.store.get(job["job_id"])
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["error_code"], "worker_interrupted")
        self.assertIsNone(self.queue.claim_next("worker-2"))

    def test_job_identifier_is_validated_before_artifact_path_creation(self):
        artifacts = LocalArtifactStore(Path(self.temp_dir.name) / "reports")
        with self.assertRaises(ValueError):
            artifacts.result_path("../../outside")


class JobWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.store = SQLiteJobStore(root / "jobs.sqlite3")
        self.queue = LocalSQLiteJobQueue(self.store)
        self.artifacts = LocalArtifactStore(root / "artifacts")

    def make_worker(self, runner=successful_runner):
        return LocalJobWorker(
            self.store,
            self.queue,
            self.artifacts,
            runner=runner,
            timeout_seconds=60,
            max_attempts=3,
        )

    @patch("backend.jobs.worker.remove_candidate_index")
    @patch("backend.jobs.worker.candidate_index_exists", return_value=False)
    def test_worker_runs_benchmark_and_persists_structured_result(self, _exists, remove_index):
        job = self.queue.submit(payload())
        worker = self.make_worker()

        self.assertTrue(worker.run_once())

        completed = self.store.get(job["job_id"])
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["result"]["before_ms"], 10.0)
        self.assertEqual(completed["result"]["after_ms"], 5.0)
        self.assertEqual(completed["artifact_ref"], self.artifacts.reference(job["job_id"]))
        self.assertTrue(self.artifacts.result_path(job["job_id"]).exists())
        remove_index.assert_called_once()
        self.assertFalse(worker.run_once())

    @patch("backend.jobs.worker.remove_candidate_index")
    @patch("backend.jobs.worker.candidate_index_exists", return_value=False)
    def test_worker_failure_is_safe_and_terminal(self, _exists, remove_index):
        job = self.queue.submit(payload())

        def failing_runner(**_kwargs):
            raise RuntimeError("private database endpoint should not reach API")

        self.assertTrue(self.make_worker(failing_runner).run_once())
        failed = self.store.get(job["job_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("private database endpoint", failed["error_message"])
        self.assertEqual(failed["error_code"], "benchmark_failed")
        remove_index.assert_called_once()

    @patch("backend.jobs.worker.remove_candidate_index")
    @patch("backend.jobs.worker.candidate_index_exists", return_value=False)
    def test_recovered_job_cleans_partial_index_before_retry(self, _exists, remove_index):
        job = self.queue.submit(payload())
        claimed = self.queue.claim_next("dead-worker")
        self.store.set_phase(job["job_id"], "dead-worker", "benchmark_started")
        runner = unittest.mock.Mock(side_effect=successful_runner)
        worker = self.make_worker(runner)

        self.assertTrue(worker.run_once())

        recovered = self.store.get(job["job_id"])
        self.assertEqual(recovered["status"], "succeeded")
        self.assertEqual(recovered["attempts"], 2)
        self.assertEqual(runner.call_count, 1)
        self.assertGreaterEqual(remove_index.call_count, 2)

    def test_worker_obeys_cross_process_lock_and_leaves_job_queued(self):
        job = self.queue.submit(payload())
        lock_path = self.store.database_path.with_name(self.store.database_path.name + ".worker.lock")
        held_lock = LocalProcessLock(lock_path)
        self.assertTrue(held_lock.acquire())
        try:
            self.assertFalse(self.make_worker().run_once())
        finally:
            held_lock.release()
        self.assertEqual(self.store.get(job["job_id"])["status"], "queued")

    def test_os_lock_excludes_a_second_process(self):
        lock_path = self.store.database_path.with_name(self.store.database_path.name + ".worker.lock")
        held_lock = LocalProcessLock(lock_path)
        self.assertTrue(held_lock.acquire())
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        process = context.Process(target=_attempt_lock_in_child, args=(str(lock_path), result_queue))
        try:
            process.start()
            process.join(timeout=10)
            self.assertFalse(process.is_alive())
            self.assertFalse(result_queue.get(timeout=2))
        finally:
            if process.is_alive():
                process.terminate()
                process.join()
            held_lock.release()
            result_queue.close()

    def test_os_lock_is_released_when_owner_process_exits(self):
        lock_path = self.store.database_path.with_name(self.store.database_path.name + ".worker.lock")
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        process = context.Process(target=_acquire_lock_and_exit, args=(str(lock_path), result_queue))
        process.start()
        process.join(timeout=10)
        try:
            self.assertFalse(process.is_alive())
            self.assertTrue(result_queue.get(timeout=2))
            recovered_lock = LocalProcessLock(lock_path)
            self.assertTrue(recovered_lock.acquire())
            recovered_lock.release()
        finally:
            if process.is_alive():
                process.terminate()
                process.join()
            result_queue.close()


class JobApiTests(unittest.TestCase):
    def test_submission_returns_queued_job_without_calling_benchmark(self):
        queued = {
            "job_id": "9460218d-7b0a-4eef-93fd-8e983d4fe13c",
            "candidate_id": "IDX_TRANSACTIONS_ACCOUNT_ID",
            "table_name": "Transactions",
            "columns": ["account_id"],
            "iterations": 10,
            "status": "queued",
            "attempts": 0,
            "created_at": "now",
            "updated_at": "now",
            "started_at": None,
            "completed_at": None,
            "error_code": None,
            "error_message": None,
            "artifact_ref": None,
        }
        queue = unittest.mock.Mock()
        queue.submit.return_value = queued
        with patch.object(benchmark_routes, "_find_recommendation", return_value=payload()), \
             patch.object(benchmark_routes, "create_queue", return_value=queue):
            response = benchmark_routes.benchmark_recommendation(
                "IDX_TRANSACTIONS_ACCOUNT_ID", iterations=10
            )

        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["job_id"], queued["job_id"])
        queue.submit.assert_called_once()
        self.assertEqual(
            benchmark_routes.router.routes[-1].status_code,
            202,
        )

    def test_unknown_recommendation_is_not_queued(self):
        with patch.object(
            benchmark_routes,
            "_find_recommendation",
            side_effect=HTTPException(status_code=404, detail="Recommendation not found"),
        ), patch.object(benchmark_routes, "create_queue") as create_queue:
            with self.assertRaises(HTTPException) as error:
                benchmark_routes.benchmark_recommendation("unknown", iterations=10)
        self.assertEqual(error.exception.status_code, 404)
        create_queue.assert_not_called()

    def test_invalid_recommendation_candidate_is_rejected_before_enqueue(self):
        recommendation = {
            **payload(),
            "candidate_id": "IDX_TRANSACTIONS_ACCOUNT_ID",
            "table": "Transactions; DROP DATABASE queryscale",
        }
        with patch.object(benchmark_routes, "_find_recommendation", return_value=recommendation), \
             patch.object(benchmark_routes, "create_queue") as create_queue:
            with self.assertRaises(HTTPException) as error:
                benchmark_routes.benchmark_recommendation(
                    "IDX_TRANSACTIONS_ACCOUNT_ID", iterations=10
                )
        self.assertEqual(error.exception.status_code, 400)
        create_queue.assert_not_called()

    def test_job_status_and_result_endpoints(self):
        active = {
            "job_id": "job-1",
            "candidate_id": "IDX_TRANSACTIONS_ACCOUNT_ID",
            "table_name": "Transactions",
            "columns": ["account_id"],
            "iterations": 10,
            "status": "running",
            "attempts": 1,
            "created_at": "now",
            "updated_at": "now",
            "started_at": "now",
            "completed_at": None,
            "error_code": None,
            "error_message": None,
            "artifact_ref": None,
            "result": None,
        }
        succeeded = {
            **active,
            "status": "succeeded",
            "result": {"before_ms": 1},
            "artifact_ref": "local-job-artifact:job-1/results.csv",
        }
        store = unittest.mock.Mock()
        with patch.object(benchmark_routes, "create_job_store", return_value=store):
            store.get.return_value = active
            response = benchmark_routes.get_benchmark_job("job-1")
            self.assertEqual(response["status"], "running")
            with self.assertRaises(HTTPException) as not_ready:
                benchmark_routes.get_benchmark_job_result("job-1")
            self.assertEqual(not_ready.exception.status_code, 409)

            store.get.return_value = succeeded
            self.assertEqual(
                benchmark_routes.get_benchmark_job_result("job-1"),
                {"before_ms": 1},
            )

            store.get.return_value = None
            with self.assertRaises(HTTPException) as missing:
                benchmark_routes.get_benchmark_job("unknown")
            self.assertEqual(missing.exception.status_code, 404)

            store.get.return_value = {**active, "status": "failed", "error_message": "safe error"}
            with self.assertRaises(HTTPException) as failed:
                benchmark_routes.get_benchmark_job_result("job-1")
            self.assertEqual(failed.exception.status_code, 409)

    def test_dashboard_contains_job_submission_and_polling_integration(self):
        dashboard_js = (Path(__file__).parents[1] / "dashboard" / "dashboard.js").read_text(encoding="utf-8")
        dashboard_html = (Path(__file__).parents[1] / "dashboard" / "index.html").read_text(encoding="utf-8")
        self.assertIn("/api/benchmark/jobs?limit=20", dashboard_js)
        self.assertIn('method: "POST"', dashboard_js)
        self.assertIn("setTimeout(pollJobs", dashboard_js)
        self.assertIn('id="benchmark-jobs"', dashboard_html)


if __name__ == "__main__":
    unittest.main()
