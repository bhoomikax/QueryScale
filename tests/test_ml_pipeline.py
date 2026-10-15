import json
import math
import tempfile
import unittest
from pathlib import Path

from backend.services.ml.dataset import (
    BENCHMARK_PROTOCOL_VERSION,
    LABEL_MEASURED,
    TARGET_NAME,
    build_measured_dataset,
    make_synthetic_dataset,
)
from backend.services.ml.evaluation import ndcg_at_k, precision_at_k, regression_metrics
from backend.services.ml.features import FEATURE_COLUMNS, candidate_feature_vector, to_feature_frame
from backend.services.ml.predictor import load_ranker, predict_benefit
from backend.services.ml.trainer import METADATA_FILE, MODEL_FILE, train_and_evaluate
from backend.services.recommender import generate_recommendations
from helpers import read_csv


def history_record(candidate_id, benefit, **overrides):
    record = {
        "run_id": f"R-{candidate_id}-{benefit}",
        "status": "COMPLETED",
        "protocol_version": BENCHMARK_PROTOCOL_VERSION,
        "candidate_id": candidate_id,
        "workload": {"signature": "W1"},
        "dataset_signature": {"hash": "D1"},
        "summary": {TARGET_NAME: benefit},
        "candidate_features": {"columns": "a", "time_share": 0.2, "equality_prefix_length": 1},
        "rule_score": 40.0,
    }
    record.update(overrides)
    return record


class FeaturePreprocessingTests(unittest.TestCase):
    def test_feature_vector_uses_nan_for_unavailable_values(self):
        vector = candidate_feature_vector({"columns": "a,b", "table_size": "", "time_share": "0.25", "write_ratio": "0"})
        self.assertEqual(list(vector), FEATURE_COLUMNS)
        self.assertTrue(math.isnan(vector["log_table_rows"]))
        self.assertEqual(vector["write_ratio"], 0.0, "a measured zero stays zero")
        self.assertEqual(vector["column_count"], 2.0)
        self.assertEqual(to_feature_frame([{}]).shape, (1, len(FEATURE_COLUMNS)))


class DatasetTests(unittest.TestCase):
    def test_only_comparable_completed_runs_are_used(self):
        history = [
            history_record("A", 0.1),
            history_record("B", 0.2),
            history_record("C", 0.3, status="FAILED"),
            history_record("D", 0.3, protocol_version=1),
            history_record("E", 0.3, workload={"signature": "OTHER"}),
            history_record("F", None),
            history_record("G", 0.1, dataset_signature={"hash": "D2"}),
        ]
        frame, provenance = build_measured_dataset(history)
        self.assertEqual(sorted(frame["candidate_id"]), ["A", "B", "G"])
        self.assertEqual(provenance["label_source"], LABEL_MEASURED)
        self.assertEqual(provenance["records_excluded"]["not_completed"], 1)
        strict, strict_provenance = build_measured_dataset(history, strict_dataset=True)
        self.assertEqual(sorted(strict["candidate_id"]), ["A", "B"])
        self.assertEqual(strict_provenance["records_excluded"]["different_dataset"], 1)

    def test_synthetic_dataset_is_labelled_and_reproducible(self):
        first, provenance = make_synthetic_dataset(n_candidates=10, runs_per_candidate=2, seed=7)
        second, _ = make_synthetic_dataset(n_candidates=10, runs_per_candidate=2, seed=7)
        self.assertTrue((first[TARGET_NAME] == second[TARGET_NAME]).all())
        self.assertEqual(set(first["label_source"]), {"synthetic_demo"})
        self.assertIn("Synthetic", provenance["warning"])


class EvaluationMetricTests(unittest.TestCase):
    def test_metrics(self):
        self.assertEqual(regression_metrics([0.0, 1.0], [0.0, 0.0]), {"mae": 0.5, "rmse": round(math.sqrt(0.5), 6)})
        self.assertEqual(ndcg_at_k([0.3, 0.1, 0.0], [3, 2, 1], 3), 1.0)
        self.assertLess(ndcg_at_k([0.3, 0.1, 0.0], [1, 2, 3], 3), 1.0)
        self.assertIsNone(ndcg_at_k([0.0, -0.1], [1, 2], 2), "no positive gain means NDCG is undefined")
        self.assertEqual(precision_at_k([0.2, 0.0, 0.1], [3, 2, 1], 2), 0.5)
        self.assertIsNone(precision_at_k([0.2], [1], 2), "fewer items than k")


class TrainingAndPredictionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_insufficient_data_reports_limitation_without_model(self):
        frame, provenance = build_measured_dataset([history_record("A", 0.1), history_record("B", 0.2), history_record("C", 0.0)])
        report = train_and_evaluate(frame, provenance, model_dir=self.dir, publish=False)
        self.assertEqual(report["status"], "insufficient_data")
        self.assertFalse((self.dir / MODEL_FILE).exists())
        self.assertIn("preliminary_rule_baseline", report)
        self.assertFalse(load_ranker(self.dir).available)

    def test_training_persists_metadata_and_synthetic_model_is_not_used_by_default(self):
        frame, provenance = make_synthetic_dataset(n_candidates=30, runs_per_candidate=2, seed=3)
        report = train_and_evaluate(frame, provenance, model_dir=self.dir, publish=False)
        self.assertEqual(report["status"], "trained")
        self.assertIn(report["selected_model"], {"ridge", "decision_tree", "random_forest", "gradient_boosting"})
        self.assertIn("dummy_mean", report["holdout_metrics"])
        # The split is grouped by candidate, so train and test groups partition the 30 candidates.
        self.assertEqual(report["train_groups"] + report["test_groups"], 30)

        metadata = json.loads((self.dir / METADATA_FILE).read_text(encoding="utf-8"))
        for key in ("model_version", "trained_at", "feature_names", "target_definition", "provenance", "metrics", "artifact_sha256"):
            self.assertIn(key, metadata)

        refused = load_ranker(self.dir)
        self.assertTrue(refused.available)
        self.assertFalse(refused.usable)
        self.assertIn("synthetic", refused.reason)

        allowed = load_ranker(self.dir, allow_synthetic=True)
        self.assertEqual(allowed.usable, metadata["suitable"])
        if allowed.usable:
            predictions = predict_benefit(allowed, [{"columns": "a", "time_share": 0.5, "equality_prefix_length": 1}, {}])
            self.assertEqual(len(predictions), 2)

    def test_tampered_or_mismatched_models_are_refused(self):
        frame, provenance = make_synthetic_dataset(n_candidates=30, runs_per_candidate=2, seed=3)
        train_and_evaluate(frame, provenance, model_dir=self.dir, publish=False)
        metadata_path = self.dir / METADATA_FILE
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

        metadata["feature_names"] = FEATURE_COLUMNS[:-1]
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        self.assertIn("feature schema", load_ranker(self.dir, allow_synthetic=True).reason)

        metadata["feature_names"] = FEATURE_COLUMNS
        metadata["artifact_sha256"] = "0" * 64
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        self.assertIn("checksum", load_ranker(self.dir, allow_synthetic=True).reason)


class RecommenderRankingTests(unittest.TestCase):
    def write_candidates(self, path):
        header = "candidate_id,table,columns,score,rule_score,recommended_action,reason,time_share,equality_prefix_length\n"
        rows = [
            "IDX_LOANS_STATUS,Loans,status,60,60,CREATE INDEX a;,r1,0.1,1\n",
            "IDX_LOANS_STATUS_PRINCIPAL,Loans,\"status, principal\",55,55,CREATE INDEX b;,r2,0.6,1\n",
            "IDX_ACCOUNTS_BALANCE,Accounts,balance,30,30,CREATE INDEX c;,r3,0.3,0\n",
            "IDX_ACCOUNTS_ACCOUNT_TYPE,Accounts,account_type,5,5,CREATE INDEX d;,r4,0.01,1\n",
        ]
        path.write_text(header + "".join(rows), encoding="utf-8")

    def test_rule_fallback_and_superseded_prefixes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidates, output = Path(tmpdir) / "c.csv", Path(tmpdir) / "r.csv"
            self.write_candidates(candidates)
            rows = generate_recommendations(candidates, output, use_ml=False)
            ids = [r["candidate_id"] for r in rows]
            self.assertEqual(ids, ["IDX_LOANS_STATUS", "IDX_LOANS_STATUS_PRINCIPAL", "IDX_ACCOUNTS_BALANCE"])
            self.assertEqual({r["ranking_method"] for r in rows}, {"rule"})
            self.assertIsNone(rows[0]["ml_predicted_benefit"])
            self.assertEqual(read_csv(output)[0]["ml_predicted_benefit"], "")

    def test_ml_ranking_reorders_and_drops_superseded_prefix(self):
        class FakeState:
            usable = True
            reason = "ok"
            version = "vTEST"
            pipeline = None

        def fake_predict(state, rows):
            return [float(r["time_share"]) for r in rows]

        from unittest import mock

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch("backend.services.ml.predictor.predict_benefit", fake_predict):
            candidates, output = Path(tmpdir) / "c.csv", Path(tmpdir) / "r.csv"
            self.write_candidates(candidates)
            rows = generate_recommendations(candidates, output, ranker=FakeState())
            ids = [r["candidate_id"] for r in rows]
            self.assertEqual(ids[0], "IDX_LOANS_STATUS_PRINCIPAL")
            self.assertNotIn("IDX_LOANS_STATUS", ids, "prefix of a higher-ranked index is superseded")
            self.assertEqual(rows[0]["ranking_method"], "ml")
            self.assertEqual(rows[0]["ml_model_version"], "vTEST")
            self.assertIn("not a measured result", rows[0]["explanation"])
            self.assertEqual(rows[0]["score"], 55.0, "score stays the heuristic rule score")

    def test_prediction_failure_falls_back_to_rules(self):
        class BrokenState:
            usable = True
            reason = "ok"
            version = "v"
            pipeline = None

        with tempfile.TemporaryDirectory() as tmpdir:
            candidates, output = Path(tmpdir) / "c.csv", Path(tmpdir) / "r.csv"
            self.write_candidates(candidates)
            rows = generate_recommendations(candidates, output, ranker=BrokenState())
            self.assertEqual(rows[0]["ranking_method"], "rule")
            self.assertIn("Prediction failed", rows[0]["ml_status"])


if __name__ == "__main__":
    unittest.main()
