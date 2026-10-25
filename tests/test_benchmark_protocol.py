"""Benchmark protocol consistency: one shared configuration for API, dashboard, CLI and pipeline runs.

No MySQL is used; benchmark execution is mocked exactly as in test_benchmark_workflow.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from backend.app import app
from backend.config import settings
from backend.services import benchmark
from backend.services.analysis import run_benchmark_job
from backend.services.benchmark import ReplayQuery, label_protocol, run_benchmark_detailed, workload_signature
from backend.services.index_manager import IndexApplication
from backend.services.ml.store import SampleStore, build_sample, measured_eligibility
from helpers import patched_settings
from test_ml_contract import benchmark_record, measured_history


class SharedProtocolTests(unittest.TestCase):
    def test_every_entry_point_defaults_to_the_same_protocol(self):
        from scripts.collect_benchmark_labels import build_parser

        protocol = label_protocol()
        self.assertEqual(protocol["iterations"], settings.benchmark_iterations)
        self.assertEqual(protocol["repeats"], settings.benchmark_repeats)
        self.assertEqual(protocol["seed"], settings.benchmark_seed)
        self.assertEqual(protocol["protocol_version"], benchmark.PROTOCOL_VERSION)
        self.assertEqual(protocol["warm_up_passes"], 1)
        self.assertEqual(len(protocol["query_ids"]), 14)
        args = build_parser().parse_args([])
        self.assertEqual((args.iterations, args.repeats), (protocol["iterations"], protocol["repeats"]))
        self.assertEqual(label_protocol()["workload_signature"], protocol["workload_signature"], "deterministic")

    def test_signature_changes_whenever_protocol_values_change(self):
        templates = {"Q1": "SELECT 1"}
        base = workload_signature(templates, 200, 42, 2)
        self.assertNotEqual(base, workload_signature(templates, 100, 42, 2))
        self.assertNotEqual(base, workload_signature(templates, 200, 42, 1))
        self.assertNotEqual(base, workload_signature(templates, 200, 7, 2))
        self.assertNotEqual(base, workload_signature({"Q1": "SELECT 2"}, 200, 42, 2))
        configured = label_protocol()["workload_signature"]
        with patched_settings(benchmark_iterations=50):
            patched = label_protocol()
        self.assertEqual(patched["iterations"], 50)
        self.assertNotEqual(patched["workload_signature"], configured, "changing BENCHMARK_ITERATIONS changes the signature")
        self.assertEqual(label_protocol()["workload_signature"], configured, "and the patch does not leak")


class RecordedProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        # run_benchmark_job writes to the configured history, results, and store paths: keep all three in the temp dir.
        self.settings_patch = patched_settings(
            ml_dataset_path=str(self.dir / "ml_dataset.sqlite3"),
            benchmark_history_path=str(self.dir / "history.jsonl"),
            benchmark_result_path=str(self.dir / "results.csv"),
        )
        self.settings_patch.__enter__()
        self.lock_patch = mock.patch.object(benchmark, "_lock_path", return_value=self.dir / "benchmark.lock")
        self.lock_patch.start()
        plan = [ReplayQuery("Q003", "SELECT 1", ()), ReplayQuery("Q001", "SELECT 2", ())]
        phase = {"explain": {"Q003": [{"key": "idx_transactions_account_id_txn_date"}], "Q001": [{"key": "PRIMARY"}]},
                 "rows_examined": {}, "write_probe": {}, "timings": {"Q003": [10.0, 9.0], "Q001": [4.0, 4.0]}}
        cursor = mock.MagicMock()
        cursor.fetchone.return_value = ("8.4.11",)
        connection = mock.MagicMock()
        connection.cursor.return_value = cursor
        application = IndexApplication("IDX_TRANSACTIONS_ACCOUNT_ID_TXN_DATE", "idx_transactions_account_id_txn_date",
                                       "Transactions", ("account_id", "txn_date"), "CREATE INDEX ...")
        self.patches = [
            mock.patch.object(benchmark, "reset_test_indexes", return_value=[]),
            mock.patch.object(benchmark, "_reference_indexes", return_value={}),
            mock.patch.object(benchmark, "connect_to_test_database", return_value=connection),
            mock.patch.object(benchmark, "_dataset_signature", return_value={"row_counts": {}, "hash": "D"}),
            mock.patch.object(benchmark, "_build_replay_plan", return_value=plan),
            mock.patch.object(benchmark, "_measure_phase", side_effect=lambda *a, **k: dict(phase)),
            mock.patch.object(benchmark, "apply_index", return_value=application),
            mock.patch.object(benchmark, "measure_index_size_bytes", return_value=None),
            mock.patch.object(benchmark, "drop_index", return_value=True),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.lock_patch.stop()
        self.settings_patch.__exit__(None, None, None)
        self.tmp.cleanup()

    def run_once(self, **kwargs):
        return run_benchmark_detailed(
            "IDX_TRANSACTIONS_ACCOUNT_ID_TXN_DATE", "Transactions", "account_id,txn_date",
            output_path=self.dir / "results.csv", history_path=self.dir / "history.jsonl",
            candidate_features={"supporting_query_ids": "Q003"}, **kwargs,
        )

    def test_default_run_uses_and_records_the_label_protocol(self):
        protocol = label_protocol()
        record = self.run_once()
        workload = record["workload"]
        self.assertEqual((workload["iterations"], workload["repeats"], workload["seed"]),
                         (protocol["iterations"], protocol["repeats"], protocol["seed"]))
        self.assertEqual(workload["signature"], protocol["workload_signature"])
        self.assertTrue(workload["matches_label_protocol"])
        self.assertEqual(record["protocol_version"], benchmark.PROTOCOL_VERSION)
        for key in ("dataset_signature", "server_version", "candidate_features", "test_database", "label_source"):
            self.assertIn(key, record)
        sample = SampleStore(self.dir / "ml_dataset.sqlite3").samples()[0]
        self.assertEqual(sample["workload_signature"], protocol["workload_signature"])
        self.assertEqual(sample["validation_status"], "valid")
        self.assertEqual(sample["baseline_workload_median_ms"], record["summary"]["baseline_workload_median_ms"])

    def test_explicit_values_are_recorded_as_a_different_protocol(self):
        record = self.run_once(iterations=5, repeats=1)
        self.assertFalse(record["workload"]["matches_label_protocol"])
        self.assertNotEqual(record["workload"]["signature"], label_protocol()["workload_signature"])
        result = run_benchmark_job({"candidate_id": "IDX_TRANSACTIONS_ACCOUNT_ID_TXN_DATE", "table": "Transactions",
                                    "columns": "account_id,txn_date", "iterations": 5, "repeats": 1})
        self.assertFalse(result["workload"]["matches_label_protocol"])
        default = run_benchmark_job({"candidate_id": "IDX_TRANSACTIONS_ACCOUNT_ID_TXN_DATE", "table": "Transactions",
                                     "columns": "account_id,txn_date"})
        self.assertTrue(default["workload"]["matches_label_protocol"])
        store = SampleStore(self.dir / "ml_dataset.sqlite3")
        self.assertEqual(len(store.status()["measured_workload_signatures"]), 2)

    def test_invalid_protocol_values_are_rejected_before_any_connection(self):
        with self.assertRaises(ValueError):
            self.run_once(iterations=0)
        with self.assertRaises(ValueError):
            self.run_once(repeats=11)
        self.assertFalse((self.dir / "history.jsonl").exists(), "rejected before the run was recorded")


class EligibilityByProtocolTests(unittest.TestCase):
    def test_only_the_dominant_protocol_counts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SampleStore(Path(tmpdir) / "s.sqlite3")
            w1 = measured_history(n_candidates=6, runs=2)                      # 12 rows, signature W1
            w2 = [benchmark_record(candidate_id=f"IDX_OTHER_{i}", run_id=f"O-{i}-{r}", workload={"signature": "W2"})
                  for i in range(5) for r in range(2)]                       # 10 rows, signature W2
            store.upsert([build_sample(r) for r in w1 + w2])
            status = store.status()
            self.assertEqual(status["by_label_source"]["measured_benchmark"]["valid_samples"], 22)
            self.assertEqual(status["comparable_workload_signature"], "W1")
            self.assertEqual(status["comparable_measured"], {"samples": 12, "candidates": 6})
            eligibility = measured_eligibility(status, 20, 5)
            self.assertFalse(eligibility["eligible"], "22 valid rows across two protocols are not 22 comparable rows")
            self.assertEqual(eligibility["measured_valid_samples"], 12)
            self.assertEqual(eligibility["measured_other_protocol_samples"], 10)
            self.assertIn("different benchmark protocol", eligibility["note"])
            frame, provenance = store.training_frame()
            self.assertEqual(len(frame), 12)
            self.assertEqual(provenance["records_excluded"]["different_workload"], 10)


class ProtocolApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.settings_patch = patched_settings(
            recommendation_path=str(self.dir / "recommendations.csv"),
            ml_dataset_path=str(self.dir / "ml_dataset.sqlite3"),
            benchmark_history_path=str(self.dir / "history.jsonl"),
        )
        self.settings_patch.__enter__()
        (self.dir / "recommendations.csv").write_text(
            "candidate_id,table,columns,score,priority,recommended_sql,reason,supporting_query_ids\n"
            "IDX_LOANS_STATUS,Loans,status,60,high,CREATE INDEX x;,r,Q009\n", encoding="utf-8",
        )
        self.client = TestClient(app)

    def tearDown(self):
        self.settings_patch.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_protocol_endpoint_reports_effective_settings(self):
        payload = self.client.get("/api/benchmark/protocol").json()
        self.assertEqual(payload["iterations"], settings.benchmark_iterations)
        self.assertEqual(payload["repeats"], settings.benchmark_repeats)
        self.assertEqual(payload["settings"]["BENCHMARK_ITERATIONS"], settings.benchmark_iterations)
        self.assertIn("workload_signature", payload)
        self.assertIsNone(payload["training_set"]["workload_signature"], "empty store")
        self.assertTrue(payload["training_set"]["matches_configured_protocol"])

    def test_api_benchmark_defaults_to_the_label_protocol_and_validates_overrides(self):
        fake = {"run_id": "BR-1", "candidate_id": "IDX_LOANS_STATUS", "status": "COMPLETED", "queries": [
            {"before_avg_ms": 2.0, "after_avg_ms": 1.0, "execution_count": 4}],
            "summary": {"workload_benefit": 0.1}, "workload": {"iterations": 200, "repeats": 2, "matches_label_protocol": True}}
        with mock.patch("backend.routes.benchmark.run_benchmark_detailed", return_value=fake) as run:
            response = self.client.post("/api/benchmark/IDX_LOANS_STATUS")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(run.call_args.kwargs["iterations"], settings.benchmark_iterations)
        self.assertEqual(run.call_args.kwargs["repeats"], settings.benchmark_repeats)
        self.assertEqual(response.json()["workload"]["matches_label_protocol"], True)
        with mock.patch("backend.routes.benchmark.run_benchmark_detailed", return_value=fake) as run:
            self.client.post("/api/benchmark/IDX_LOANS_STATUS", params={"iterations": 25, "repeats": 3})
        self.assertEqual((run.call_args.kwargs["iterations"], run.call_args.kwargs["repeats"]), (25, 3))
        self.assertEqual(self.client.post("/api/benchmark/IDX_LOANS_STATUS", params={"iterations": 0}).status_code, 422)
        self.assertEqual(self.client.post("/api/benchmark/IDX_LOANS_STATUS", params={"repeats": 99}).status_code, 422)
        self.assertEqual(self.client.post("/api/benchmark/IDX_LOANS_STATUS", params={"iterations": "many"}).status_code, 422)

    def test_dashboard_shows_the_protocol(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "dashboard" / "dashboard.js").read_text(encoding="utf-8")
        html = (root / "dashboard" / "index.html").read_text(encoding="utf-8")
        self.assertIn("/api/benchmark/protocol", script)
        self.assertIn("matches_label_protocol", script)
        self.assertIn('id="benchmark-protocol"', html)
        self.assertIn("/api/benchmark/protocol", set(app.openapi()["paths"]))


if __name__ == "__main__":
    unittest.main()
