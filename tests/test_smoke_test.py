"""scripts/aws/smoke_test.py against a fake API that speaks this branch's job contract."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("smoke_test", ROOT / "scripts" / "aws" / "smoke_test.py")
smoke_test = importlib.util.module_from_spec(SPEC)
sys.modules["smoke_test"] = smoke_test
SPEC.loader.exec_module(smoke_test)

RUN_ID = "BR-20261019T000000Z-abcdef12"
SUMMARY = {"execution_count": 20, "before_ms": 8.0, "after_ms": 5.5, "speedup": 1.45, "improvement_percentage": 31.2,
           "workload_benefit": 0.36, "affected_queries": ["Q011"]}


class FakeApi(BaseHTTPRequestHandler):
    """Minimal stand-in for the FastAPI app. ``outcome`` chooses COMPLETED or FAILED."""

    outcome = "COMPLETED"
    polls = 0

    def log_message(self, *_args):  # keep test output clean
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/health/live",):
            return self._send(200, {"status": "ok"})
        if path == "/health/ready":
            return self._send(200, {"status": "ok", "ranking_method": "ml", "jobs": {"execution_mode": "queue", "worker": "running"}})
        if path == "/api/ml/model":
            return self._send(200, {"ranking_method": "ml", "production": {"usable": True, "label_source": "measured_benchmark", "reason": "Model loaded and validated."}})
        if path == "/api/recommendations":
            return self._send(200, [{"candidate_id": "IDX_LOANS_STATUS_PRINCIPAL"}, {"candidate_id": "IDX_ACCOUNTS_BALANCE"}])
        if path == "/api/jobs":
            return self._send(200, [{"job_id": "benchmark-abc", "kind": "benchmark", "status": "RUNNING"}])
        if path == "/api/jobs/benchmark-abc":
            FakeApi.polls += 1
            if FakeApi.polls < 2:
                return self._send(200, {"job_id": "benchmark-abc", "status": "RUNNING", "result": None, "error": None})
            if FakeApi.outcome == "FAILED":
                return self._send(200, {"job_id": "benchmark-abc", "status": "FAILED", "result": None,
                                        "error": {"code": "test_database_missing", "message": "no test db"}})
            return self._send(200, {"job_id": "benchmark-abc", "status": "COMPLETED", "error": None, "result": {
                "run_id": RUN_ID, "candidate_id": "IDX_LOANS_STATUS_PRINCIPAL", "summary": SUMMARY,
                "index_size_bytes": 180224, "cleanup": {"status": "dropped", "index_name": "idx_loans_status_principal"}}})
        if path == f"/api/benchmark/history/{RUN_ID}":
            return self._send(200, {"run_id": RUN_ID, "status": "COMPLETED", "protocol_version": 2,
                                    "queries": [{"query_id": "Q009"}], "cleanup": {"status": "dropped"}})
        if path == "/api/benchmark/results":
            return self._send(200, [{"candidate_id": "IDX_LOANS_STATUS_PRINCIPAL", "query_id": "Q009", "run_id": RUN_ID}])
        return self._send(404, {"detail": "not found"})

    def do_POST(self):
        if self.path.startswith("/api/benchmark/IDX_LOANS_STATUS_PRINCIPAL") and "background=true" in self.path:
            return self._send(202, {"job_id": "benchmark-abc", "kind": "benchmark", "status": "PENDING", "created": True})
        return self._send(404, {"detail": "not found"})


class SmokeTestTests(unittest.TestCase):
    def setUp(self):
        FakeApi.polls = 0
        FakeApi.outcome = "COMPLETED"
        self.server = HTTPServer(("127.0.0.1", 0), FakeApi)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _run(self, *extra):
        out = io.StringIO()
        with redirect_stdout(out):
            code = smoke_test.main(["--base-url", self.base, "--poll-interval", "0.01", "--timeout", "5", *extra])
        return code, out.getvalue(), None

    def test_completed_job_passes_with_local_artifacts(self):
        code, output, _ = self._run("--expect-local-artifacts")
        self.assertEqual(code, 0, output)
        for name in ("health /health/live", "health /health/ready", "ml model status", "submit returns 202 job",
                     "job reached terminal state", "result has aggregate fields", "cleanup recorded",
                     "run record persisted", "measurements in /api/benchmark/results", "artifact reference"):
            self.assertIn(f"[PASS       ] {name}", output)
        self.assertIn("0 failed, 0 pending", output)

    def test_missing_s3_artifacts_are_pending_not_failed(self):
        code, output, _ = self._run()
        self.assertEqual(code, 1, "pending application work blocks a green result by default")
        self.assertIn("[PENDING-APP] artifact reference", output)
        self.assertIn("0 failed, 1 pending", output)
        code, output, _ = self._run("--tolerate-missing-adapters")
        self.assertEqual(code, 0, output)

    def test_failed_job_is_reported_as_failure(self):
        FakeApi.outcome = "FAILED"
        code, output, _ = self._run("--expect-local-artifacts")
        self.assertEqual(code, 1)
        self.assertIn("[FAIL       ] job reached terminal state", output)
        self.assertIn("test_database_missing", output)


if __name__ == "__main__":
    unittest.main()
