"""Deployment-only settings ported from the cloud branch: TLS, benchmark user, liveness route.

Local defaults must stay unchanged so the Docker Compose workflow keeps working.
No test here connects to MySQL. Settings fields are evaluated at import time, so the
tests build variants with ``dataclasses.replace`` instead of mutating the environment.
"""

import dataclasses
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from backend.app import app
from backend.config import Settings


ROOT = Path(__file__).resolve().parents[1]
BASE = Settings()


class CloudSettingsTests(unittest.TestCase):
    def test_local_defaults_have_no_tls_and_reuse_primary_credentials(self):
        settings = dataclasses.replace(
            BASE, db_user="root", db_password="x", db_test_user="root", db_test_password="x",
            db_tls_verify=False, db_tls_ca="",
        )
        config = settings.mysql_config()
        self.assertNotIn("ssl_verify_cert", config)
        self.assertNotIn("ssl_ca", config)
        self.assertEqual(config["connection_timeout"], settings.db_connection_timeout)
        test = settings.test_mysql_config()
        self.assertEqual(test["database"], settings.db_test_name)
        self.assertEqual((test["user"], test["password"]), ("root", "x"))

    def test_test_user_falls_back_to_primary_user_when_unset(self):
        # The class default reads DB_TEST_USER or DB_USER; with neither set it is "root".
        self.assertTrue(BASE.db_test_user)
        self.assertEqual(Settings.__dataclass_fields__["db_test_user"].default, BASE.db_test_user)

    def test_deployment_settings_enable_tls_and_benchmark_user(self):
        settings = dataclasses.replace(
            BASE, db_user="queryscale_app", db_password="a", db_test_user="queryscale_benchmark", db_test_password="b",
            db_tls_verify=True, db_tls_ca="/app/certs/rds-global-bundle.pem", api_host="0.0.0.0", api_port=8080,
        )
        config = settings.mysql_config()
        self.assertTrue(config["ssl_verify_cert"])
        self.assertTrue(config["ssl_verify_identity"])
        self.assertEqual(config["ssl_ca"], "/app/certs/rds-global-bundle.pem")
        self.assertEqual(config["user"], "queryscale_app")
        test = settings.test_mysql_config()
        self.assertEqual((test["user"], test["password"], test["database"]), ("queryscale_benchmark", "b", settings.db_test_name))
        self.assertEqual((settings.api_host, settings.api_port), ("0.0.0.0", 8080))

    def test_index_manager_connects_with_the_benchmark_user(self):
        from backend.services import index_manager
        from helpers import patched_settings

        with patched_settings(db_user="app", db_password="a", db_test_user="bench", db_test_password="b",
                              db_name="queryscale", db_test_name="queryscale_test"), \
                mock.patch("mysql.connector.connect") as connect:
            index_manager.connect_to_test_database()
        kwargs = connect.call_args.kwargs
        self.assertEqual((kwargs["user"], kwargs["password"], kwargs["database"]), ("bench", "b", "queryscale_test"))


class LivenessRouteTests(unittest.TestCase):
    def test_health_live_alias_matches_health(self):
        client = TestClient(app)
        self.assertEqual(client.get("/health/live").json(), {"status": "ok"})
        self.assertEqual(client.get("/health").json(), {"status": "ok"})


class ImageContractTests(unittest.TestCase):
    """The Dockerfile and Terraform must agree with the application they run."""

    def test_dockerfile_matches_ecs_assumptions(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("/health/live", dockerfile)
        self.assertIn("rds-global-bundle.pem", dockerfile)
        self.assertIn("${API_PORT:-8000}", dockerfile)
        self.assertIn("COPY --chown=10001:10001 data ./data", dockerfile)

    def test_terraform_worker_command_is_this_branch_worker(self):
        services = (ROOT / "infra" / "terraform" / "ecs_services.tf").read_text(encoding="utf-8")
        self.assertIn('["python", "-m", "backend.worker"]', services)
        self.assertNotIn("backend.jobs.worker", services)
        self.assertTrue((ROOT / "backend" / "worker.py").exists())
        self.assertFalse((ROOT / "backend" / "jobs").exists(), "the cloud branch's duplicate job system is not merged")

    def test_terraform_api_task_runs_jobs_itself_with_benchmark_credentials(self):
        """ECS tasks do not share a filesystem, so the API task runs jobs in thread mode and
        needs the benchmark user's secret; the execution role must be allowed to read it."""
        services = (ROOT / "infra" / "terraform" / "ecs_services.tf").read_text(encoding="utf-8")
        api_env = services.split("api_environment = merge(")[1].split("api_secrets = merge(")[0]
        self.assertIn('JOB_EXECUTION_MODE = "thread"', api_env)
        self.assertIn("DB_TEST_USER = var.db_benchmark_username", api_env)
        api_secrets = services.split("api_secrets = merge(")[1].split("worker_environment = merge(")[0]
        self.assertIn('DB_TEST_PASSWORD = "${local.secret_db_benchmark.arn}:password::"', api_secrets)
        iam = (ROOT / "infra" / "terraform" / "iam.tf").read_text(encoding="utf-8")
        api_role = iam.split("api = {")[1].split("worker = {")[0]
        self.assertIn("local.secret_db_benchmark.arn", api_role)
        tfvars = (ROOT / "infra" / "terraform" / "environments" / "dev.tfvars.example").read_text(encoding="utf-8")
        self.assertIn("worker_desired_count = 0", tfvars)


if __name__ == "__main__":
    unittest.main()
