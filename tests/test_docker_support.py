"""Docker-related behavior: database-name safeguards, schema targeting, queue mode, and the worker.

No test here connects to MySQL.
"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from backend import worker
from backend.services.jobs import COMPLETED, FAILED, PENDING, RUNNING, JobManager
from backend.services.test_database import assert_distinct_databases
from helpers import patched_settings


ROOT = Path(__file__).resolve().parents[1]


class DatabaseNameSafeguardTests(unittest.TestCase):
    def test_names_differing_only_by_case_are_rejected(self):
        for primary, test in [("queryscale", "QueryScale"), ("queryscale", "queryscale"), ("QS", "qs ")]:
            with self.assertRaises(ValueError, msg=(primary, test)):
                assert_distinct_databases(primary, test)
        self.assertEqual(assert_distinct_databases("queryscale", "queryscale_test"), ("queryscale", "queryscale_test"))

    def test_invalid_identifiers_are_rejected(self):
        with self.assertRaises(ValueError):
            assert_distinct_databases("queryscale", "test`; DROP DATABASE queryscale")

    def test_clone_and_test_connections_refuse_case_collisions_before_connecting(self):
        from backend.services.index_manager import connect_to_test_database
        from backend.services.test_database import clone_test_database

        with patched_settings(db_name="queryscale", db_test_name="QUERYSCALE"), mock.patch("mysql.connector.connect") as connect:
            with self.assertRaises(ValueError):
                clone_test_database()
            with self.assertRaises(ValueError):
                connect_to_test_database()
            connect.assert_not_called()

    def test_pipeline_validates_names_before_destructive_steps(self):
        from scripts import run_local_pipeline

        with patched_settings(db_name="queryscale", db_test_name="QueryScale"), \
                mock.patch.object(run_local_pipeline, "initialize_schema") as init, \
                mock.patch.object(run_local_pipeline, "seed_source_database") as seed:
            with self.assertRaises(ValueError):
                run_local_pipeline.run_local_pipeline(10, True, {}, *[Path("x")] * 5, None)
            init.assert_not_called()
            seed.assert_not_called()


class SchemaTargetingTests(unittest.TestCase):
    def test_schema_statements_target_configured_database(self):
        from scripts.run_local_pipeline import schema_statements

        statements = schema_statements("bank_dev")
        self.assertEqual(statements[1], "USE `bank_dev`")
        self.assertTrue(statements[0].startswith("CREATE DATABASE IF NOT EXISTS `bank_dev`"))
        joined = "\n".join(statements)
        self.assertNotIn("`queryscale`", joined)
        self.assertEqual(sum(s.startswith("CREATE TABLE") for s in statements), 5)

    def test_database_level_statements_in_schema_are_refused(self):
        from scripts.run_local_pipeline import schema_statements

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "schema.sql"
            path.write_text("DROP DATABASE queryscale;\nCREATE TABLE t (id INT);", encoding="utf-8")
            with self.assertRaises(ValueError):
                schema_statements("bank_dev", path)


class QueueModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "runtime" / "jobs.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_api_records_and_worker_executes(self):
        api = JobManager(self.path, mode="queue")
        executed = []
        job, created = api.submit("analysis", "k", {"n": 1}, lambda params: executed.append("api") or {})
        self.assertTrue(created)
        self.assertEqual(job["status"], PENDING)
        self.assertEqual(executed, [], "queue mode must not execute in the API process")

        duplicate, created_again = api.submit("analysis", "k", {"n": 1}, lambda params: {})
        self.assertFalse(created_again)
        self.assertEqual(duplicate["job_id"], job["job_id"])

        executor = JobManager(self.path, mode="queue", recover_interrupted=False)
        claimed = executor.claim_next()
        self.assertEqual(claimed["status"], RUNNING)
        self.assertIsNone(executor.claim_next(), "a claimed job is not handed out twice")

        # A restarted API in queue mode must not fail the job the worker is running.
        JobManager(self.path, mode="queue")
        self.assertEqual(api.get(job["job_id"])["status"], RUNNING)

        finished = executor.run_claimed(claimed, lambda params: {"echo": params["n"]})
        self.assertEqual(finished["status"], COMPLETED)
        self.assertEqual(finished["result"], {"echo": 1})

    def test_worker_run_once_uses_registered_handlers(self):
        manager = JobManager(self.path, mode="queue")
        self.assertFalse(worker.run_once(manager))
        manager.submit("training", "t", {"seed": 1}, lambda params: {})
        manager.submit("mystery", "m", {}, lambda params: {})
        with mock.patch.dict(worker.JOB_HANDLERS, {"training": lambda params: {"trained": params["seed"]}}):
            self.assertTrue(worker.run_once(manager))
            self.assertTrue(worker.run_once(manager))
        jobs = {job["kind"]: job for job in manager.list()}
        self.assertEqual(jobs["training"]["status"], COMPLETED)
        self.assertEqual(jobs["training"]["result"], {"trained": 1})
        self.assertEqual(jobs["mystery"]["status"], FAILED)
        self.assertEqual(jobs["mystery"]["error"]["code"], "unknown_job_kind")

    def test_restarted_worker_fails_only_running_jobs(self):
        manager = JobManager(self.path, mode="queue")
        first, _ = manager.submit("analysis", "a", {}, lambda params: {})
        manager.claim_next()
        second, _ = manager.submit("benchmark", "b", {}, lambda params: {})
        self.assertEqual(manager.recover_interrupted(include_pending=False), 1)
        self.assertEqual(manager.get(first["job_id"])["error"]["code"], "interrupted")
        self.assertEqual(manager.get(second["job_id"])["status"], PENDING)

    def test_only_one_worker_lock(self):
        lock_path = Path(self.tmp.name) / "worker.lock"
        first, second = worker.WorkerLock(lock_path), worker.WorkerLock(lock_path)
        self.assertTrue(first.acquire())
        try:
            self.assertFalse(second.acquire())
        finally:
            first.release()
        self.assertTrue(second.acquire())
        second.release()

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            JobManager(self.path, mode="celery")


class ReadinessTests(unittest.TestCase):
    def test_queue_mode_reports_missing_worker(self):
        from backend.app import app

        with tempfile.TemporaryDirectory() as tmpdir:
            job_db = str(Path(tmpdir) / "jobs.sqlite3")
            with patched_settings(job_execution_mode="queue", job_database_path=job_db, model_dir=str(Path(tmpdir) / "m")):
                ready = TestClient(app).get("/health/ready").json()
                self.assertEqual(ready["status"], "degraded")
                self.assertIsNone(ready["jobs"]["worker_heartbeat_age_seconds"])
                (Path(tmpdir) / "worker.heartbeat").write_text(str(time.time()), encoding="utf-8")
                ready = TestClient(app).get("/health/ready").json()
                self.assertEqual(ready["jobs"]["worker"], "running")


class DockerConfigurationTests(unittest.TestCase):
    def test_compose_and_dockerfile_contain_no_secrets_and_use_service_hostname(self):
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        # Compare against the developer's local password, if any, without hardcoding or printing it.
        from dotenv import dotenv_values

        env_path = ROOT / ".env"
        local_password = (dotenv_values(env_path).get("DB_PASSWORD") or "") if env_path.exists() else ""
        if local_password and local_password != "change_me":
            for name, text in (("compose.yaml", compose), ("Dockerfile", dockerfile), (".env.example", example)):
                self.assertFalse(local_password in text, f"the local DB_PASSWORD value appears in {name}")
        self.assertIn("DB_PASSWORD=change_me", example)
        self.assertIn("DB_HOST: queryscale-mysql", compose)
        self.assertIn('DB_PORT: "3306"', compose)
        self.assertIn("${DB_PASSWORD:?", compose)
        copy_lines = [line for line in dockerfile.splitlines() if line.strip().upper().startswith(("COPY", "ADD"))]
        self.assertFalse([line for line in copy_lines if ".env" in line or line.split()[1] == "."], copy_lines)
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".env", dockerignore)
        self.assertIn(".env.*", dockerignore)

    def test_env_example_covers_settings_used_by_the_application(self):
        import dataclasses
        import re

        from backend.config import Settings

        source = (ROOT / "backend" / "config.py").read_text(encoding="utf-8")
        used = set(re.findall(r'os\.getenv\("([A-Z0-9_]+)"', source))
        example = {line.split("=", 1)[0] for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines() if "=" in line and not line.startswith("#")}
        self.assertTrue(dataclasses.is_dataclass(Settings))
        self.assertEqual(sorted(used - example), [])


if __name__ == "__main__":
    unittest.main()
