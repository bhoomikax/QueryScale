"""API-level ML tests: eligibility, dataset status, synthetic labelling, ML ranking and rule fallback.

Runs the real FastAPI application with temporary artifact paths. No MySQL.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from backend.app import app
from backend.services import jobs
from backend.services.jobs import JobManager
from backend.services.ml.dataset import LABEL_MEASURED, LABEL_SYNTHETIC
from backend.services.ml.store import SampleStore
from backend.services.ml.trainer import METADATA_FILE, MODEL_FILE
from backend.services.recommender import generate_recommendations
from helpers import patched_settings
from test_ml_contract import measured_history


ROOT = Path(__file__).resolve().parents[1]

CANDIDATE_HEADER = (
    "candidate_id,table,columns,score,rule_score,recommended_action,reason,time_share,workload_share,"
    "equality_prefix_length,column_count,average_execution_time,table_size,leading_column_cardinality,existing_prefix_length\n"
)
CANDIDATE_ROWS = [
    "IDX_TRANSACTIONS_C0,Transactions,col0,60,60,CREATE INDEX a;,r1,0.05,0.04,0,1,1.5,1000,100,0\n",
    "IDX_TRANSACTIONS_C5,Transactions,col5,45,45,CREATE INDEX b;,r2,0.60,0.48,1,1,7.0,100000,10000,0\n",
    "IDX_ACCOUNTS_BALANCE,Accounts,balance,30,30,CREATE INDEX c;,r3,0.30,0.24,0,1,4.0,15000,9000,0\n",
]


class MlApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
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
            "ml_dataset_path": "ml_dataset.sqlite3",
        }
        overrides = {key: str(self.dir / value) for key, value in paths.items()}
        overrides["metadata_source"] = "schema"
        self.settings_patch = patched_settings(**overrides)
        self.settings_patch.__enter__()
        jobs.set_job_manager(JobManager(self.dir / "jobs.sqlite3", run_inline=True))
        self.client = TestClient(app)
        (self.dir / "index_candidates.csv").write_text(CANDIDATE_HEADER + "".join(CANDIDATE_ROWS), encoding="utf-8")

    def tearDown(self):
        jobs.set_job_manager(None)
        self.settings_patch.__exit__(None, None, None)
        self.tmp.cleanup()

    def write_history(self, records):
        path = self.dir / "benchmark_history.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    def test_dataset_and_model_status_without_any_data(self):
        dataset = self.client.get("/api/ml/dataset").json()
        self.assertEqual(dataset["store"]["total_samples"], 0)
        self.assertEqual(dataset["eligibility"]["status"], "insufficient_measured_data")
        self.assertEqual(dataset["eligibility"]["fallback"], "rule_based_ranking")
        self.assertFalse(dataset["history_file"]["exists"])
        model = self.client.get("/api/ml/model").json()
        self.assertEqual(model["ranking_method"], "rule")
        self.assertEqual(model["eligibility"]["mode"], "rule_fallback")
        self.assertFalse(model["production"]["available"])
        features = self.client.get("/api/ml/features").json()
        self.assertEqual(features["count"], 21)
        self.assertEqual(len(features["features"]), 21)
        self.assertEqual(features["target"]["name"], "workload_benefit")
        ready = self.client.get("/health/ready").json()
        self.assertEqual(ready["ml"]["status"], "insufficient_measured_data")

    def test_synthetic_demo_is_persisted_labelled_and_never_counts_as_measured(self):
        job = self.client.post("/api/ml/train", json={"synthetic_demo": True}).json()
        self.assertEqual(job["status"], "COMPLETED", job)
        self.assertEqual(job["result"]["mode"], "synthetic_demo")
        self.assertEqual(job["result"]["label_source"], LABEL_SYNTHETIC)
        self.assertEqual(job["result"]["status"], "trained")

        dataset = self.client.get("/api/ml/dataset").json()
        by_source = dataset["store"]["by_label_source"]
        self.assertGreater(by_source[LABEL_SYNTHETIC]["samples"], 0)
        self.assertEqual(by_source[LABEL_MEASURED]["samples"], 0)
        self.assertEqual(dataset["eligibility"]["status"], "insufficient_measured_data")

        model = self.client.get("/api/ml/model").json()
        self.assertEqual(model["ranking_method"], "rule", "a synthetic model never ranks by default")
        self.assertIsNotNone(model["demo"])
        self.assertEqual(model["demo"]["label_source"], LABEL_SYNTHETIC)
        self.assertEqual(model["demo"]["provenance_label"], "synthetic-trained")
        self.assertIn("Synthetic", model["demo"]["note"])
        self.assertFalse(model["production"]["available"])

        evaluation = self.client.get("/api/ml/evaluation").json()
        self.assertIsNone(evaluation["production"])
        self.assertEqual(evaluation["demo"]["label_source"], LABEL_SYNTHETIC)
        self.assertTrue(any("synthetic" in item.lower() for item in evaluation["demo"]["limitations"]))
        rows = {row["model"]: row for row in evaluation["comparison"]["demo"]}
        self.assertEqual(set(rows), {"dummy_mean", "ridge", "decision_tree", "random_forest", "gradient_boosting"})
        self.assertEqual(sum(1 for row in rows.values() if row["selected"]), 1)
        self.assertEqual(rows["dummy_mean"]["role"], "baseline")

        samples = self.client.get("/api/ml/dataset/samples", params={"label_source": LABEL_SYNTHETIC, "limit": 5}).json()
        self.assertEqual(len(samples), 5)
        self.assertEqual(samples[0]["label_source"], LABEL_SYNTHETIC)
        self.assertNotIn("raw_snapshot", samples[0])

    def test_measured_training_activates_ml_and_fallback_returns_on_corruption(self):
        self.write_history(measured_history(n_candidates=6, runs=4))
        dataset = self.client.post("/api/ml/dataset/sync").json()
        self.assertEqual(dataset["sync"]["inserted"], 24)
        self.assertEqual(self.client.post("/api/ml/dataset/sync").json()["sync"]["inserted"], 0, "idempotent")
        status = self.client.get("/api/ml/dataset").json()
        self.assertEqual(status["eligibility"]["status"], "no_model")
        self.assertTrue(status["eligibility"]["measured_data"]["eligible"])

        job = self.client.post("/api/ml/train", json={}).json()
        self.assertEqual(job["status"], "COMPLETED", job)
        result = job["result"]
        self.assertEqual(result["mode"], "measured")
        self.assertEqual(result["status"], "trained")
        self.assertEqual(result["label_source"], LABEL_MEASURED)
        self.assertEqual(result["sample_counts"], {"measured": 24, "synthetic": 0})
        self.assertTrue(result["suitable"], result)
        self.assertTrue(result["model_replaced"])
        self.assertEqual(result["reranked"]["ranking_method"], "ml")

        model = self.client.get("/api/ml/model").json()
        self.assertEqual(model["ranking_method"], "ml")
        self.assertEqual(model["eligibility"]["status"], "measured_model_active")
        self.assertEqual(model["production"]["label_source"], LABEL_MEASURED)
        self.assertEqual(model["production"]["provenance_label"], "measured-trained")
        self.assertEqual(model["production"]["validation_status"], "usable")
        self.assertEqual(model["production"]["sample_counts"]["synthetic"], 0)

        recommendations = self.client.get("/api/recommendations").json()
        self.assertTrue(recommendations)
        first = recommendations[0]
        self.assertEqual(first["ranking_method"], "ml")
        self.assertEqual(first["ml_label_source"], LABEL_MEASURED)
        self.assertEqual(first["ml_provenance_label"], "measured-trained")
        self.assertIsNotNone(first["ml_predicted_benefit"])
        self.assertEqual(first["ml_prediction_type"], "model_prediction")
        self.assertEqual(first["score_type"], "heuristic_rule_score")
        self.assertIn("predicted workload_benefit", first["ranking_reason"])
        self.assertEqual(first["candidate_id"], "IDX_TRANSACTIONS_C5", "highest time share is predicted most beneficial")

        candidates = self.client.get("/api/candidates").json()
        self.assertTrue(all(c["ml_predicted_benefit"] is not None for c in candidates))
        self.assertEqual(candidates[0]["ml_label_source"], LABEL_MEASURED)
        self.assertEqual(self.client.get("/health/ready").json()["ml"]["status"], "measured_model_active")

        # Corrupt the model file: the API reports the rejection and ranking falls back to rules.
        model_dir = self.dir / "models"
        (model_dir / MODEL_FILE).write_bytes(b"corrupt")
        model = self.client.get("/api/ml/model").json()
        self.assertEqual(model["ranking_method"], "rule")
        self.assertEqual(model["eligibility"]["status"], "model_rejected")
        self.assertIn("checksum", model["eligibility"]["reason"])
        generate_recommendations(self.dir / "index_candidates.csv", self.dir / "recommendations.csv")
        fallback = self.client.get("/api/recommendations").json()
        self.assertEqual(fallback[0]["ranking_method"], "rule")
        self.assertIsNone(fallback[0]["ml_predicted_benefit"])
        self.assertIn("checksum", fallback[0]["ml_status"])
        self.assertEqual(fallback[0]["candidate_id"], "IDX_TRANSACTIONS_C0", "rule score order again")
        candidates = self.client.get("/api/candidates").json()
        self.assertTrue(all(c["ml_predicted_benefit"] is None for c in candidates))
        self.assertIn("checksum", candidates[0]["ml_status"])

    def test_restart_keeps_samples_and_model(self):
        self.write_history(measured_history(n_candidates=6, runs=4))
        job = self.client.post("/api/ml/train", json={}).json()
        self.assertEqual(job["result"]["status"], "trained", job)
        version = job["result"]["model_version"]
        before = self.client.get("/api/ml/dataset").json()["store"]["total_samples"]

        # "Restart": a fresh job manager and client against the same files, plus a reseed of MySQL
        # would not touch any of these paths. An insufficient retrain must not discard anything.
        jobs.set_job_manager(JobManager(self.dir / "jobs.sqlite3", run_inline=True))
        client = TestClient(app)
        self.assertEqual(client.get("/api/ml/dataset").json()["store"]["total_samples"], before)
        self.assertEqual(client.get("/api/ml/model").json()["production"]["model_version"], version)
        store = SampleStore(self.dir / "ml_dataset.sqlite3")
        self.assertEqual(len(store.samples(valid_only=True)), 24)

        with patched_settings(ml_min_training_groups=50):
            retry = client.post("/api/ml/train", json={"seed": 7}).json()
        self.assertEqual(retry["result"]["status"], "insufficient_data")
        self.assertEqual(retry["result"]["active_model_version"], version)
        self.assertEqual(client.get("/api/ml/model").json()["production"]["model_version"], version)
        self.assertTrue((self.dir / "models" / METADATA_FILE).exists())

    def test_training_request_validation(self):
        self.assertEqual(self.client.post("/api/ml/train", json={"seed": -1}).status_code, 422)
        self.assertEqual(self.client.get("/api/ml/dataset/samples", params={"label_source": "bogus"}).status_code, 422)


class BenchmarkSafetyTests(unittest.TestCase):
    def test_index_changes_are_refused_when_test_database_equals_source(self):
        from backend.services.index_manager import apply_index, drop_index, reset_test_indexes

        with patched_settings(db_name="queryscale", db_test_name="QUERYSCALE"), mock.patch("mysql.connector.connect") as connect:
            with self.assertRaises(ValueError):
                apply_index("IDX_LOANS_STATUS", "Loans", "status")
            with self.assertRaises(ValueError):
                drop_index("idx_loans_status", "Loans")
            with self.assertRaises(ValueError):
                reset_test_indexes({})
            connect.assert_not_called()


class DashboardLabellingTests(unittest.TestCase):
    def test_dashboard_distinguishes_synthetic_from_measured_and_uses_the_ml_endpoints(self):
        script = (ROOT / "dashboard" / "dashboard.js").read_text(encoding="utf-8")
        html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
        css = (ROOT / "dashboard" / "style.css").read_text(encoding="utf-8")
        for endpoint in ("/api/ml/dataset", "/api/ml/model", "/api/ml/evaluation", "/api/ml/train"):
            self.assertIn(endpoint, script)
        self.assertIn('makeTag("synthetic"', script, "synthetic predictions and models get their own tag")
        self.assertIn(".tag-synthetic", css)
        self.assertIn("synthetic-trained", script)
        self.assertIn("measured-trained", script)
        self.assertIn("not evidence of real index recommendation quality", script)
        self.assertIn('id="train-model"', html)
        self.assertIn('id="train-demo"', html)


if __name__ == "__main__":
    unittest.main()
