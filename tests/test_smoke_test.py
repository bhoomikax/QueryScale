"""Drive scripts/aws/smoke_test.py against a stub API so its logic is covered without AWS."""

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

SMOKE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "aws" / "smoke_test.py"
spec = importlib.util.spec_from_file_location("smoke_test", SMOKE_PATH)
smoke_test = importlib.util.module_from_spec(spec)
sys.modules["smoke_test"] = smoke_test  # dataclasses resolve deferred annotations via sys.modules
spec.loader.exec_module(smoke_test)  # type: ignore[union-attr]

JOB_ID = "9460218d-7b0a-4eef-93fd-8e983d4fe13c"


class StubApi:
    """Configurable fake of the QueryScale API. `scenario` controls the job outcome."""

    def __init__(self, scenario: str):
        self.scenario = scenario
        self.polls = 0
        self.server = HTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def job(self) -> dict:
        self.polls += 1
        status = "running" if self.polls == 1 else ("failed" if self.scenario == "fails" else "succeeded")
        ref = "s3://bucket/jobs/%s/results.csv" % JOB_ID if self.scenario.startswith("cloud") else "local-job-artifact:%s/results.csv" % JOB_ID
        return {
            "job_id": JOB_ID, "candidate_id": "IDX_TRANSACTIONS_ACCOUNT_ID", "table": "Transactions",
            "columns": ["account_id"], "iterations": 5, "status": status, "attempts": 1,
            "created_at": "t", "updated_at": "t", "started_at": "t", "completed_at": "t",
            "error_code": "benchmark_failed" if status == "failed" else None,
            "error": "boom" if status == "failed" else None,
            "artifact_ref": ref if status == "succeeded" else None,
            "result_url": f"/api/benchmark/jobs/{JOB_ID}/result" if status == "succeeded" else None,
        }

    def result(self) -> dict:
        base = {"before_ms": 10.0, "after_ms": 5.0, "speedup": 2.0, "improvement_percentage": 50.0,
                "execution_count": 4, "results": [{"query_id": "Q001", "execution_count": 4}]}
        if self.scenario == "cloud-verified":
            base["cleanup_verified"] = True
        return base

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):  # silence
                pass

            def _send(self, code, payload, raw=False):
                body = payload if raw else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/csv" if raw else "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path in ("/health/live", "/health/ready"):
                    return self._send(200, {"status": "ok"})
                if path == "/api/recommendations":
                    return self._send(200, [{"candidate_id": "IDX_TRANSACTIONS_ACCOUNT_ID"}])
                if path == "/api/benchmark/jobs":
                    return self._send(200, [stub.job()] if stub.polls else [])
                if path == f"/api/benchmark/jobs/{JOB_ID}":
                    return self._send(200, stub.job())
                if path == f"/api/benchmark/jobs/{JOB_ID}/result":
                    return self._send(200, stub.result())
                if path == "/api/benchmark/results":
                    return self._send(200, [{"job_id": JOB_ID, "query_id": "Q001"}])
                if path == f"/api/benchmark/jobs/{JOB_ID}/artifact":
                    if stub.scenario == "cloud-verified":
                        return self._send(200, {"url": f"{stub.url}/presigned"})
                    return self._send(404, {"detail": "Not Found"})
                if path == "/presigned":
                    return self._send(200, b"candidate_id,query_id,before_avg_ms\nIDX,Q001,1.0\n", raw=True)
                return self._send(404, {"detail": "Not Found"})

            def do_POST(self):
                if self.path.startswith("/api/benchmark/IDX_TRANSACTIONS_ACCOUNT_ID"):
                    return self._send(202, {"job_id": JOB_ID, "status": "queued"})
                return self._send(404, {"detail": "Not Found"})

        return Handler


def run(stub: StubApi, *extra: str) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = smoke_test.main(["--base-url", stub.url, "--poll-interval", "0.01", "--timeout", "5", *extra])
    return code, out.getvalue()


class SmokeTestTests(unittest.TestCase):
    def test_local_run_passes_with_tolerated_pending_checks(self):
        stub = StubApi("local").start()
        try:
            code, out = run(stub, "--expect-local-artifacts", "--tolerate-missing-adapters")
        finally:
            stub.stop()
        self.assertEqual(code, 0, out)
        self.assertIn("[PASS       ] submit returns 202 queued", out)
        self.assertIn("[PASS       ] artifact reference: local-job-artifact", out)
        self.assertIn("[PENDING-APP] cleanup_verified", out)
        self.assertIn("[PASS       ] measurements merged", out)

    def test_pending_checks_fail_the_run_unless_tolerated(self):
        stub = StubApi("local").start()
        try:
            code, out = run(stub, "--expect-local-artifacts")
        finally:
            stub.stop()
        self.assertEqual(code, 1)
        self.assertIn("Pending application features block a green result", out)

    def test_cloud_run_reports_missing_artifact_endpoint_as_pending(self):
        stub = StubApi("cloud").start()
        try:
            code, out = run(stub)
        finally:
            stub.stop()
        self.assertEqual(code, 1)
        self.assertIn("[PASS       ] artifact reference: s3://", out)
        self.assertIn("[PENDING-APP] artifact download", out)
        self.assertIn("[PENDING-APP] cleanup_verified", out)

    def test_fully_implemented_cloud_run_is_green(self):
        stub = StubApi("cloud-verified").start()
        try:
            code, out = run(stub)
        finally:
            stub.stop()
        self.assertEqual(code, 0, out)
        self.assertIn("[PASS       ] cleanup_verified", out)
        self.assertIn("[PASS       ] artifact download", out)
        self.assertIn("0 failed, 0 pending", out)

    def test_failed_job_is_a_failure(self):
        stub = StubApi("fails").start()
        try:
            code, out = run(stub, "--expect-local-artifacts", "--tolerate-missing-adapters")
        finally:
            stub.stop()
        self.assertEqual(code, 1)
        self.assertIn("[FAIL       ] job reached terminal state", out)
        self.assertIn("benchmark_failed", out)


if __name__ == "__main__":
    unittest.main()
