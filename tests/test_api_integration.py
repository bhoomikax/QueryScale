import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from backend.app import app
from backend.services import jobs
from backend.services.jobs import JobManager
from helpers import log_rows, patched_settings, write_log


ROOT = Path(__file__).resolve().parents[1]


class ApiIntegrationTests(unittest.TestCase):
    """Runs the real analysis pipeline on a temporary log through the API. No database is used."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        rows = log_rows("Q003", "SELECT * FROM Transactions WHERE account_id = %s ORDER BY txn_date DESC", [3.0] * 6, rows_examined=40)
        rows += log_rows("Q009", "SELECT loan_id FROM Loans WHERE status = %s ORDER BY principal DESC LIMIT 50", [8.0] * 4)
        write_log(self.dir / "query_logs.csv", rows)
        paths = {
            "query_log_path": "query_logs.csv",
            "query_feature_path": "query_features.csv",
            "feature_path": "candidate_features.csv",
            "candidate_path": "index_candidates.csv",
            "recommendation_path": "recommendations.csv",
            "benchmark_result_path": "benchmark_results.csv",
            "benchmark_history_path": "benchmark_history.jsonl",
            "schema_metadata_path": "missing_snapshot.json",
            "model_dir": "models",
        }
        overrides = {key: str(self.dir / value) for key, value in paths.items()}
        overrides["metadata_source"] = "schema"
        self.settings_patch = patched_settings(**overrides)
        self.settings_patch.__enter__()
        jobs.set_job_manager(JobManager(self.dir / "jobs.sqlite3", run_inline=True))
        self.client = TestClient(app)

    def tearDown(self):
        jobs.set_job_manager(None)
        self.settings_patch.__exit__(None, None, None)
        self.tmp.cleanup()

    def analyze(self):
        response = self.client.post("/api/analysis", json={"metadata_source": "schema"})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def test_analysis_job_produces_recommendations_with_explanations(self):
        job = self.analyze()
        self.assertEqual(job["status"], "COMPLETED", job)
        self.assertEqual(job["result"]["metadata_source"], "schema")
        self.assertEqual(self.client.get("/api/analysis/status").json()["job_id"], job["job_id"])
        self.assertEqual(self.client.get(f"/api/jobs/{job['job_id']}").json()["status"], "COMPLETED")

        recommendations = self.client.get("/api/recommendations").json()
        self.assertTrue(recommendations)
        first = recommendations[0]
        for key in ("candidate_id", "table", "columns", "score", "priority", "recommended_sql", "reason"):
            self.assertIn(key, first, "legacy dashboard field")
        self.assertIsInstance(first["columns"], list)
        self.assertEqual(first["ranking_method"], "rule")
        self.assertIsNone(first["ml_predicted_benefit"])
        self.assertEqual(first["score_type"], "heuristic_rule_score")
        self.assertTrue(first["positive_evidence"])
        self.assertIn("table row count", first["unavailable_metrics"], "schema.sql has no row counts")

        detail = self.client.get(f"/api/recommendations/{first['candidate_id']}").json()
        self.assertEqual(detail["supporting_queries"][0]["query_id"], first["supporting_query_ids"][0])

        candidates = self.client.get("/api/candidates", params={"include_covered": True}).json()
        self.assertTrue(any(c["existing_index"] for c in candidates))

    def test_query_summary_reports_unavailable_metrics_as_null(self):
        summary = {row["query_id"]: row for row in self.client.get("/api/queries/summary").json()}
        self.assertEqual(summary["Q003"]["rows_examined_avg"], 40.0)
        self.assertIsNone(summary["Q009"]["rows_examined_avg"])
        self.assertIsNone(summary["Q009"]["p95_latency_ms"])
        self.assertEqual(self.client.get("/api/queries").json()[0]["rows_examined"], 40.0)

    def test_background_benchmark_job_and_history(self):
        self.analyze()
        candidate_id = self.client.get("/api/recommendations").json()[0]["candidate_id"]
        fake_record = {
            "run_id": "BR-1", "candidate_id": candidate_id, "status": "COMPLETED",
            "summary": {"workload_benefit": 0.12, "affected_queries": ["Q003"]},
            "index_size_bytes": 1024, "cleanup": {"status": "dropped"},
        }
        with mock.patch("backend.services.benchmark.run_benchmark_detailed", return_value=fake_record) as run:
            response = self.client.post(f"/api/benchmark/{candidate_id}", params={"background": True, "iterations": 10})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "COMPLETED")
        self.assertEqual(response.json()["result"]["summary"]["workload_benefit"], 0.12)
        self.assertEqual(run.call_args.kwargs["iterations"], 10)

        history_path = self.dir / "benchmark_history.jsonl"
        history_path.write_text(json.dumps({**fake_record, "write_overhead": {"insert_overhead_pct": 3.0, "before": {}}}) + "\n", encoding="utf-8")
        history = self.client.get("/api/benchmark/history").json()
        self.assertEqual(history[0]["write_overhead"], {"insert_overhead_pct": 3.0})
        self.assertEqual(self.client.get("/api/benchmark/history/BR-1").json()["run_id"], "BR-1")
        self.assertEqual(self.client.get("/api/benchmark/history/missing").status_code, 404)

    def test_benchmark_errors_map_to_status_codes(self):
        self.analyze()
        candidate_id = self.client.get("/api/recommendations").json()[0]["candidate_id"]
        from backend.services.index_manager import BenchmarkError

        with mock.patch("backend.routes.benchmark.run_benchmark_detailed", side_effect=BenchmarkError("test_database_missing", "missing")):
            response = self.client.post(f"/api/benchmark/{candidate_id}")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "test_database_missing")
        self.assertEqual(self.client.post("/api/benchmark/IDX_NOPE").status_code, 404)

    def test_benchmark_results_keep_missing_values_null(self):
        with (self.dir / "benchmark_results.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["candidate_id", "query_id", "before_avg_ms", "after_avg_ms", "execution_count", "before_p95_ms", "speedup", "improvement_percentage", "affected"])
            writer.writerow(["IDX", "Q1", "2.0", "1.0", "4", "", "2.0", "50.0", "True"])
        rows = self.client.get("/api/benchmark/results").json()
        self.assertIsNone(rows[0]["before_p95_ms"])
        self.assertEqual(rows[0]["execution_count"], 4)
        self.assertTrue(rows[0]["affected"])
        self.assertEqual(rows[0]["measurement_type"], "measured")

    def test_ml_endpoints_without_a_model(self):
        model = self.client.get("/api/ml/model").json()
        self.assertEqual(model["ranking_method"], "rule")
        self.assertFalse(model["production"]["available"])
        self.assertIsNone(model["demo"])
        self.assertEqual(self.client.get("/api/ml/evaluation").status_code, 404)

        job = self.client.post("/api/ml/train", json={}).json()
        self.assertEqual(job["status"], "COMPLETED")
        self.assertEqual(job["result"]["status"], "insufficient_data")
        evaluation = self.client.get("/api/ml/evaluation").json()
        self.assertEqual(evaluation["production"]["status"], "insufficient_data")

    def test_health_and_validation(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        ready = self.client.get("/health/ready").json()
        self.assertEqual(ready["ranking_method"], "rule")
        self.assertEqual(self.client.post("/api/analysis", json={"metadata_source": "bogus"}).status_code, 422)
        self.assertEqual(self.client.get("/api/jobs/does-not-exist").status_code, 404)


class DashboardCompatibilityTests(unittest.TestCase):
    def test_dashboard_assets_reference_existing_endpoints(self):
        client = TestClient(app)
        html = client.get("/dashboard/").text
        for element_id in ("recommendation-list", "performance-chart", "ml-evaluation", "run-analysis", "workspace-state"):
            self.assertIn(f'id="{element_id}"', html)
        script = (ROOT / "dashboard" / "dashboard.js").read_text(encoding="utf-8")
        paths = set(app.openapi()["paths"])
        for endpoint in ("/api/queries", "/api/queries/slow", "/api/recommendations", "/api/benchmark/results",
                         "/api/benchmark/history", "/api/ml/model", "/api/ml/evaluation", "/api/analysis/status", "/api/analysis"):
            self.assertIn(endpoint, script)
            self.assertIn(endpoint, paths)


if __name__ == "__main__":
    unittest.main()
