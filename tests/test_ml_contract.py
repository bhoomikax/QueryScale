"""Contract tests for the ML pipeline: feature schema, dataset store, labels, training, artifacts.

Nothing here connects to MySQL. Benchmark records are built in memory in the exact
shape ``backend.services.benchmark.run_benchmark_detailed`` writes to the history file.
"""

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
from sklearn.model_selection import GroupShuffleSplit

from backend.services.artifact_store import sha256_file
from backend.services.benchmark import summarize_workload
from backend.services.ml import trainer as trainer_module
from backend.services.ml.dataset import LABEL_MEASURED, LABEL_SYNTHETIC, TARGET_NAME, make_synthetic_dataset
from backend.services.ml.evaluation import ndcg_at_k, precision_at_k, regression_metrics, spearman
from backend.services.ml.features import (
    FEATURE_COLUMNS,
    FEATURE_COUNT,
    FEATURE_DESCRIPTIONS,
    FEATURE_SCHEMA_VERSION,
    FEATURE_SOURCES,
    FeatureSchemaError,
    candidate_feature_vector,
    to_feature_frame,
    validate_feature_frame,
)
from backend.services.ml.predictor import load_ranker, predict_benefit
from backend.services.ml.store import (
    EXCLUDED,
    VALID,
    SampleStore,
    build_sample,
    candidate_fingerprint,
    measured_eligibility,
    sample_id_for,
)
from backend.services.ml.trainer import (
    BASELINE_MODEL,
    METADATA_FILE,
    MODEL_FILE,
    REJECTED_DIR,
    TrainingInProgress,
    candidate_models,
    prepare_training_frame,
    train_and_evaluate,
    training_lock,
)


EXPECTED_FEATURES = [
    "column_count", "equality_prefix_length", "has_range_column", "covers_order_by", "covers_group_by",
    "is_covering", "supporting_query_count", "workload_share", "time_share", "log_avg_latency_ms",
    "log_median_latency_ms", "log_p95_latency_ms", "where_usage_ratio", "join_usage_ratio", "order_usage_ratio",
    "log_table_rows", "leading_selectivity", "log_leading_cardinality", "write_ratio", "existing_prefix_ratio",
    "log_estimated_index_mib",
]


def snapshot(**overrides):
    """A candidate row as written to data/index_candidates.csv (all values are CSV strings)."""

    row = {
        "candidate_id": "IDX_TRANSACTIONS_ACCOUNT_ID_TXN_DATE", "table": "Transactions", "columns": "account_id, txn_date",
        "column_count": "2", "equality_prefix_length": "1", "has_range_column": "false", "covers_order_by": "true",
        "covers_group_by": "false", "is_covering": "false", "supporting_query_ids": "Q003", "supporting_query_count": "1",
        "supporting_observations": "40", "frequency": "40", "workload_share": "0.2", "time_share": "0.3",
        "average_execution_time": "2.5", "median_execution_time": "2.0", "p95_execution_time": "4.0",
        "where_usage": "40", "join_usage": "0", "order_usage": "40", "table_size": "100000",
        "leading_column_cardinality": "15000", "leading_column_selectivity": "0.15", "write_ratio": "0.01",
        "existing_prefix_length": "1", "existing_prefix_index": "idx_transactions_account_id",
        "estimated_index_bytes": "3686400", "rule_score": "43.7", "score": "43.7",
    }
    row.update({k: str(v) for k, v in overrides.items()})
    return row


def benchmark_record(candidate_id="IDX_TRANSACTIONS_ACCOUNT_ID_TXN_DATE", run_id="BR-1", benefit=0.12, **overrides):
    """A COMPLETED protocol-2 run record in the shape run_benchmark_detailed persists."""

    record = {
        "run_id": run_id, "protocol_version": 2, "status": "COMPLETED", "started_at": "2026-10-24T19:00:00Z",
        "finished_at": "2026-10-24T19:00:30Z", "candidate_id": candidate_id, "table": "Transactions",
        "columns": ["account_id", "txn_date"], "index_name": "idx_transactions_account_id_txn_date",
        "index_sql": "CREATE INDEX `idx_transactions_account_id_txn_date` ON `Transactions` (`account_id`, `txn_date`)",
        "test_database": "queryscale_test", "label_source": LABEL_MEASURED, "server_version": "8.4.11",
        "dataset_signature": {"row_counts": {"Transactions": 100000}, "hash": "D1"},
        "workload": {"iterations": 200, "seed": 42, "repeats": 2, "signature": "W1"},
        "supporting_query_ids": ["Q003"], "candidate_features": snapshot(candidate_id=candidate_id),
        "rule_score": "43.7", "cleanup": {"status": "dropped"},
        "summary": {
            TARGET_NAME: benefit, "affected_queries": ["Q003"], "execution_count": 400,
            "before_ms": 5.0, "after_ms": 4.0, "baseline_workload_median_ms": 1800.0, "indexed_workload_median_ms": 1600.0,
        },
    }
    record.update(overrides)
    return record


def measured_history(n_candidates=6, runs=4, seed=1):
    """Comparable COMPLETED runs whose label is a clean function of the features plus small noise."""

    rng = np.random.default_rng(seed)
    records = []
    for c in range(n_candidates):
        time_share = 0.05 + 0.55 * c / max(1, n_candidates - 1)
        eq_prefix = c % 2
        features = snapshot(
            candidate_id=f"IDX_TRANSACTIONS_C{c}", columns=f"col{c}", column_count=1, equality_prefix_length=eq_prefix,
            time_share=round(time_share, 4), workload_share=round(time_share * 0.8, 4),
            average_execution_time=round(1 + 10 * time_share, 3), table_size=10 ** (3 + c % 3),
            leading_column_cardinality=10 ** (2 + c % 3), existing_prefix_length=0,
        )
        for r in range(runs):
            label = 0.5 * time_share + 0.05 * eq_prefix + float(rng.normal(0, 0.004))
            records.append(benchmark_record(
                candidate_id=f"IDX_TRANSACTIONS_C{c}", run_id=f"BR-C{c}-R{r}", benefit=round(label, 6),
                columns=[f"col{c}"], candidate_features=features, rule_score=str(30 + 10 * time_share),
            ))
    return records


class FeatureSchemaTests(unittest.TestCase):
    def test_exactly_21_features_in_fixed_order(self):
        self.assertEqual(FEATURE_COUNT, 21)
        self.assertEqual(FEATURE_COLUMNS, EXPECTED_FEATURES)
        self.assertEqual(len(set(FEATURE_COLUMNS)), 21, "no duplicate feature names")
        self.assertEqual(set(FEATURE_DESCRIPTIONS), set(FEATURE_COLUMNS))
        self.assertEqual(set(FEATURE_SOURCES), set(FEATURE_COLUMNS))
        self.assertEqual(FEATURE_SCHEMA_VERSION, 1)

    def test_vector_and_frame_follow_the_schema(self):
        vector = candidate_feature_vector(snapshot())
        self.assertEqual(list(vector), FEATURE_COLUMNS)
        self.assertEqual(vector["column_count"], 2.0)
        self.assertEqual(vector["existing_prefix_ratio"], 0.5)
        self.assertAlmostEqual(vector["log_avg_latency_ms"], math.log1p(2.5))
        self.assertAlmostEqual(vector["where_usage_ratio"], 1.0)
        frame = to_feature_frame([snapshot(), {}])
        self.assertEqual(list(frame.columns), FEATURE_COLUMNS)
        self.assertEqual(frame.shape, (2, 21))
        self.assertTrue((frame.dtypes == float).all())

    def test_training_and_inference_derive_identical_vectors(self):
        """The snapshot stored with a benchmark run and the live candidate row give the same features."""

        record = benchmark_record()
        from_store = build_sample(record)
        stored = json.loads(from_store["features"])
        live = candidate_feature_vector(snapshot())
        for name in FEATURE_COLUMNS:
            if stored[name] is None:
                self.assertTrue(math.isnan(live[name]), name)
            else:
                self.assertAlmostEqual(stored[name], live[name], places=9, msg=name)
        frame = to_feature_frame([snapshot()])
        self.assertEqual(list(frame.columns), FEATURE_COLUMNS)

    def test_invalid_missing_and_non_finite_inputs_become_nan_or_errors(self):
        vector = candidate_feature_vector(snapshot(
            table_size="inf", time_share="nan", average_execution_time="abc", write_ratio="-inf",
            leading_column_cardinality="-5", estimated_index_bytes="", supporting_observations="0",
        ))
        for name in ("log_table_rows", "time_share", "log_avg_latency_ms", "write_ratio",
                     "log_leading_cardinality", "log_estimated_index_mib", "where_usage_ratio"):
            self.assertTrue(math.isnan(vector[name]), name)
        self.assertEqual(vector["column_count"], 2.0, "derived from the column list when column_count is unusable")
        with self.assertRaises(FeatureSchemaError):
            candidate_feature_vector(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_frame_validation_rejects_schema_mismatches_with_useful_messages(self):
        good = to_feature_frame([snapshot()])
        validate_feature_frame(good)
        with self.assertRaises(FeatureSchemaError) as missing:
            validate_feature_frame(good.drop(columns=["write_ratio"]))
        self.assertIn("missing ['write_ratio']", str(missing.exception))
        with self.assertRaises(FeatureSchemaError) as extra:
            validate_feature_frame(good.assign(bogus=1.0))
        self.assertIn("unexpected ['bogus']", str(extra.exception))
        with self.assertRaises(FeatureSchemaError) as order:
            validate_feature_frame(good[list(reversed(FEATURE_COLUMNS))])
        self.assertIn("wrong order", str(order.exception))
        with self.assertRaises(FeatureSchemaError) as types:
            validate_feature_frame(good.assign(write_ratio="text"))
        self.assertIn("non-numeric", str(types.exception))
        with_inf = good.copy()
        with_inf.loc[0, "time_share"] = np.inf
        self.assertTrue(math.isnan(validate_feature_frame(with_inf).loc[0, "time_share"]))


class SampleStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "ml_dataset.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_sample_identity_and_fingerprint(self):
        self.assertEqual(sample_id_for("BR-1", "IDX_A"), sample_id_for("BR-1", "IDX_A"))
        self.assertNotEqual(sample_id_for("BR-1", "IDX_A"), sample_id_for("BR-2", "IDX_A"))
        self.assertEqual(candidate_fingerprint("Transactions", "account_id, txn_date"),
                         candidate_fingerprint("transactions", ["ACCOUNT_ID", "txn_date"]))
        self.assertNotEqual(candidate_fingerprint("Transactions", "account_id"), candidate_fingerprint("Transactions", "txn_date"))

    def test_samples_persist_across_reopen_and_restart(self):
        SampleStore(self.path).record_benchmark(benchmark_record())
        reopened = SampleStore(self.path)
        rows = reopened.samples()
        self.assertEqual(len(rows), 1)
        sample = rows[0]
        self.assertEqual(sample["validation_status"], VALID)
        self.assertEqual(sample["target"], 0.12)
        self.assertEqual(sample["label_source"], LABEL_MEASURED)
        self.assertEqual(sample["baseline_workload_median_ms"], 1800.0)
        self.assertEqual(sample["indexed_workload_median_ms"], 1600.0)
        self.assertEqual(sample["affected_queries"], ["Q003"])
        self.assertEqual(sample["feature_schema_version"], FEATURE_SCHEMA_VERSION)
        self.assertEqual(list(sample["features"]), FEATURE_COLUMNS)
        self.assertIn("CREATE INDEX", sample["index_definition"])

    def test_ingest_is_idempotent_and_keeps_distinct_runs(self):
        history = self.dir / "history.jsonl"
        records = [benchmark_record(run_id="BR-1", benefit=0.10), benchmark_record(run_id="BR-2", benefit=0.11)]
        history.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        store = SampleStore(self.path)
        first = store.sync_history(history)
        second = store.sync_history(history)
        self.assertEqual((first["inserted"], first["updated"]), (2, 0))
        self.assertEqual((second["inserted"], second["updated"]), (0, 2), "re-sync updates in place, never duplicates")
        self.assertEqual(len(store.samples()), 2, "two distinct runs of one candidate stay two observations")
        store.record_benchmark(records[0])
        self.assertEqual(len(store.samples()), 2)
        self.assertEqual(store.status()["by_label_source"][LABEL_MEASURED]["distinct_candidates"], 1)

    def test_invalid_or_incomplete_benchmarks_are_excluded_with_reasons(self):
        store = SampleStore(self.path)
        store.upsert([
            build_sample(benchmark_record(run_id="ok")),
            build_sample(benchmark_record(run_id="failed", status="FAILED", summary=None)),
            build_sample(benchmark_record(run_id="old", protocol_version=1)),
            build_sample(benchmark_record(run_id="nosnap", candidate_features={})),
            build_sample(benchmark_record(run_id="nolabel", benefit=None)),
            build_sample(benchmark_record(run_id="nanlabel", benefit=float("nan"))),
            build_sample(benchmark_record(run_id="nobase", summary={TARGET_NAME: 0.1, "baseline_workload_median_ms": 0.0})),
        ])
        by_run = {s["run_id"]: s for s in store.samples()}
        self.assertEqual(by_run["ok"]["validation_status"], VALID)
        self.assertEqual(by_run["failed"]["exclusion_reason"], "benchmark_not_completed")
        self.assertEqual(by_run["old"]["exclusion_reason"], "protocol_version_mismatch")
        self.assertEqual(by_run["nosnap"]["exclusion_reason"], "missing_feature_snapshot")
        self.assertEqual(by_run["nolabel"]["exclusion_reason"], "missing_label")
        self.assertEqual(by_run["nanlabel"]["exclusion_reason"], "missing_label")
        self.assertIsNone(by_run["nanlabel"]["target"])
        self.assertEqual(by_run["nobase"]["exclusion_reason"], "no_baseline_latency")
        status = store.status()["by_label_source"][LABEL_MEASURED]
        self.assertEqual((status["valid_samples"], status["excluded_samples"]), (1, 6))
        frame, provenance = store.training_frame()
        self.assertEqual(len(frame), 1)
        self.assertEqual(provenance["records_excluded"]["missing_label"], 2)

    def test_synthetic_rows_never_count_as_measured(self):
        store = SampleStore(self.path)
        synthetic, _ = make_synthetic_dataset(n_candidates=30, runs_per_candidate=2, seed=3)
        store.record_synthetic(synthetic, seed=3)
        store.record_benchmark(benchmark_record())
        status = store.status()
        self.assertEqual(status["by_label_source"][LABEL_SYNTHETIC]["samples"], 60)
        self.assertEqual(status["by_label_source"][LABEL_MEASURED]["valid_samples"], 1)
        eligibility = measured_eligibility(status, min_samples=20, min_groups=5)
        self.assertFalse(eligibility["eligible"])
        self.assertEqual(eligibility["status"], "insufficient_measured_data")
        self.assertEqual(eligibility["synthetic_samples"], 60)
        measured_frame, _ = store.training_frame(LABEL_MEASURED)
        synthetic_frame, _ = store.training_frame(LABEL_SYNTHETIC)
        self.assertEqual(set(measured_frame["label_source"]), {LABEL_MEASURED})
        self.assertEqual(set(synthetic_frame["label_source"]), {LABEL_SYNTHETIC})
        self.assertEqual(len(synthetic_frame), 60)

    def test_only_comparable_workloads_are_combined(self):
        store = SampleStore(self.path)
        store.upsert([build_sample(r) for r in measured_history(n_candidates=3, runs=2)])
        store.upsert([build_sample(benchmark_record(run_id="other", workload={"signature": "OTHER"}))])
        frame, provenance = store.training_frame()
        self.assertEqual(len(frame), 6)
        self.assertEqual(provenance["workload_signature"], "W1")
        self.assertEqual(provenance["records_excluded"]["different_workload"], 1)

    def test_reset_requires_confirmation_and_history_survives(self):
        store = SampleStore(self.path)
        store.record_benchmark(benchmark_record())
        with self.assertRaises(ValueError):
            store.reset()
        self.assertEqual(store.reset(confirm=True), 1)
        self.assertEqual(store.samples(), [])


class LabelTests(unittest.TestCase):
    def test_workload_benefit_follows_the_documented_formula(self):
        rows = [
            {"query_id": "A", "before_median_ms": 10.0, "after_median_ms": 2.0, "before_avg_ms": 10.0, "after_avg_ms": 2.0, "affected": True},
            {"query_id": "B", "before_median_ms": 5.0, "after_median_ms": 1.0, "before_avg_ms": 5.0, "after_avg_ms": 1.0, "affected": False},
            {"query_id": "C", "before_median_ms": 4.0, "after_median_ms": 6.0, "before_avg_ms": 4.0, "after_avg_ms": 6.0, "affected": True},
        ]
        summary = summarize_workload(rows, {"A": 2, "B": 2, "C": 1})
        # affected gain = 2*(10-2) + 1*(4-6) = 14; baseline = 2*10 + 2*5 + 1*4 = 34
        self.assertAlmostEqual(summary["workload_benefit"], 14 / 34, places=6)
        self.assertEqual(summary["affected_queries"], ["A", "C"])
        self.assertEqual(summary["baseline_workload_median_ms"], 34.0)
        no_effect = summarize_workload([dict(rows[1], affected=False)], {"B": 3})
        self.assertEqual(no_effect["workload_benefit"], 0.0, "an index no query uses has zero benefit, not an invented gain")
        with self.assertRaises(ValueError):
            summarize_workload(rows, {"A": 0, "B": 0, "C": 0})

    def test_training_frame_preparation_drops_non_finite_targets_only(self):
        frame, _ = make_synthetic_dataset(n_candidates=5, runs_per_candidate=1, seed=1)
        frame.loc[0, TARGET_NAME] = np.nan
        frame.loc[1, TARGET_NAME] = np.inf
        prepared, dropped = prepare_training_frame(frame)
        self.assertEqual(len(prepared), 3)
        self.assertEqual(dropped["non_finite_target"], 2)
        with self.assertRaises(FeatureSchemaError):
            prepare_training_frame(frame.drop(columns=["group"]))


class MetricTests(unittest.TestCase):
    def test_regression_metrics(self):
        self.assertEqual(regression_metrics([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]), {"mae": 0.0, "rmse": 0.0})
        self.assertEqual(regression_metrics([0.0, 0.0], [3.0, -3.0]), {"mae": 3.0, "rmse": 3.0})
        self.assertEqual(regression_metrics([0.0, 0.0], [1.0, 3.0]), {"mae": 2.0, "rmse": round(math.sqrt(5), 6)})

    def test_ranking_metrics_and_undefined_cases(self):
        self.assertEqual(spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertEqual(spearman([1, 2, 3, 4], [40, 30, 20, 10]), -1.0)
        self.assertIsNone(spearman([1, 1, 1], [1, 2, 3]), "constant target has no rank correlation")
        self.assertIsNone(spearman([1, 2], [1, 2]), "fewer than three items")
        self.assertEqual(ndcg_at_k([0.3, 0.1, 0.0], [3, 2, 1], 3), 1.0)
        self.assertIsNone(ndcg_at_k([-0.1, 0.0], [1, 2], 2))
        self.assertEqual(precision_at_k([0.2, 0.0, 0.1], [3, 2, 1], 2), 0.5)
        self.assertIsNone(precision_at_k([0.01, 0.02], [1, 2], 2), "nothing relevant")


class TrainingProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.frame, self.provenance = make_synthetic_dataset(n_candidates=30, runs_per_candidate=2, seed=3)

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_candidate_model_fits_and_predicts(self):
        x = self.frame[FEATURE_COLUMNS]
        y = self.frame[TARGET_NAME]
        models = candidate_models(42)
        self.assertEqual(set(models), {BASELINE_MODEL, "ridge", "decision_tree", "random_forest", "gradient_boosting"})
        for name, pipeline in models.items():
            predictions = pipeline.fit(x, y).predict(x)
            self.assertEqual(predictions.shape, (len(x),), name)
            self.assertTrue(np.isfinite(predictions).all(), name)

    def test_split_is_grouped_and_baseline_uses_training_rows_only(self):
        report = train_and_evaluate(self.frame, self.provenance, model_dir=self.dir, publish=False, random_state=42)
        self.assertEqual(report["status"], "trained")
        self.assertEqual(report["train_groups"] + report["test_groups"], 30)
        self.assertEqual(report["baseline_fitted_on"], "training split only")
        # Reproduce the split exactly and verify no candidate leaks and the baseline is the train mean.
        frame, _ = prepare_training_frame(self.frame)
        groups = frame["group"].astype(str)
        splitter = GroupShuffleSplit(n_splits=1, test_size=0.25, random_state=42)
        train_index, test_index = next(splitter.split(frame[FEATURE_COLUMNS], frame[TARGET_NAME], groups))
        self.assertFalse(set(groups.iloc[train_index]) & set(groups.iloc[test_index]))
        y_train, y_test = frame[TARGET_NAME].iloc[train_index], frame[TARGET_NAME].iloc[test_index]
        expected_baseline_mae = float(np.mean(np.abs(y_test - y_train.mean())))
        self.assertAlmostEqual(report["holdout_metrics"][BASELINE_MODEL]["mae"], expected_baseline_mae, places=5)
        self.assertNotAlmostEqual(y_train.mean(), frame[TARGET_NAME].mean(), places=6, msg="test rows would change the mean")
        self.assertIn(report["selected_model"], {"ridge", "decision_tree", "random_forest", "gradient_boosting"})
        self.assertIn("training_config", report)
        self.assertEqual(report["training_config"]["random_state"], 42)
        self.assertIn("ridge", report["cv_mae"])

    def test_artifact_round_trip_checksum_and_metadata(self):
        report = train_and_evaluate(self.frame, self.provenance, model_dir=self.dir, publish=False)
        self.assertTrue(report["model_replaced"])
        metadata = json.loads((self.dir / METADATA_FILE).read_text(encoding="utf-8"))
        for key in ("model_id", "model_name", "trained_at", "feature_names", "feature_schema_version", "dataset_fingerprint",
                    "samples", "groups", "sample_counts", "training_config", "selection_basis", "metrics", "label_source",
                    "artifact_sha256", "environment", "suitable"):
            self.assertIn(key, metadata, key)
        self.assertEqual(metadata["artifact_sha256"], sha256_file(self.dir / MODEL_FILE))
        self.assertEqual(metadata["sample_counts"], {"measured": 0, "synthetic": 60})
        self.assertEqual(metadata["metrics"]["baseline_model"], BASELINE_MODEL)
        self.assertFalse(list(self.dir.glob(".staging-*")), "staging directory is removed after activation")
        first = load_ranker(self.dir, allow_synthetic=True)
        second = load_ranker(self.dir, allow_synthetic=True)
        self.assertTrue(first.usable, first.reason)
        rows = [snapshot(), snapshot(time_share="0.6", equality_prefix_length="2"), {}]
        self.assertEqual(predict_benefit(first, rows), predict_benefit(second, rows))
        self.assertEqual(len(predict_benefit(first, rows)), 3)

    def test_corrupt_or_missing_artifacts_fall_back_gracefully(self):
        train_and_evaluate(self.frame, self.provenance, model_dir=self.dir, publish=False)
        model_path = self.dir / MODEL_FILE
        metadata_path = self.dir / METADATA_FILE
        model_path.write_bytes(b"this is not a joblib file")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["artifact_sha256"] = sha256_file(model_path)  # checksum matches the garbage on purpose
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        state = load_ranker(self.dir, allow_synthetic=True)
        self.assertTrue(state.available)
        self.assertFalse(state.usable)
        self.assertIn("could not be loaded", state.reason)
        with self.assertRaises(RuntimeError):
            predict_benefit(state, [snapshot()])
        metadata["feature_schema_version"] = 99
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        self.assertIn("schema version", load_ranker(self.dir, allow_synthetic=True).reason)
        model_path.unlink()
        missing = load_ranker(self.dir, allow_synthetic=True)
        self.assertFalse(missing.available)
        self.assertIn("No trained model", missing.reason)
        metadata_path.write_text("{not json", encoding="utf-8")
        model_path.write_bytes(b"x")
        self.assertIn("unreadable", load_ranker(self.dir, allow_synthetic=True).reason)

    def test_minimum_data_rules_are_enforced(self):
        small = self.frame.head(12)  # 6 candidates, 12 rows
        report = train_and_evaluate(small, self.provenance, model_dir=self.dir, publish=False, min_samples=20, min_groups=5)
        self.assertEqual(report["status"], "insufficient_data")
        self.assertEqual(report["minimums"], {"samples": 20, "groups": 5})
        self.assertFalse((self.dir / MODEL_FILE).exists())
        report = train_and_evaluate(self.frame.head(20), self.provenance, model_dir=self.dir, publish=False, min_samples=20, min_groups=11)
        self.assertEqual(report["status"], "insufficient_data", "10 candidates is below the group minimum")
        report = train_and_evaluate(self.frame.head(20), self.provenance, model_dir=self.dir, publish=False, min_samples=20, min_groups=5)
        self.assertEqual(report["status"], "trained")

    def test_model_that_does_not_beat_baseline_is_rejected_and_last_valid_model_is_kept(self):
        good = train_and_evaluate(self.frame, self.provenance, model_dir=self.dir, publish=False)
        active = good["model_version"]
        self.assertTrue(load_ranker(self.dir, allow_synthetic=True).usable)

        from sklearn.dummy import DummyRegressor
        from sklearn.pipeline import Pipeline

        def hopeless_models(random_state=42):
            models = candidate_models(random_state)
            bad = Pipeline([("impute", trainer_module.build_imputer()), ("model", DummyRegressor(strategy="constant", constant=10.0))])
            return {BASELINE_MODEL: models[BASELINE_MODEL], "ridge": bad}

        other, provenance = make_synthetic_dataset(n_candidates=30, runs_per_candidate=2, seed=9)
        with mock.patch.object(trainer_module, "candidate_models", hopeless_models):
            report = train_and_evaluate(other, provenance, model_dir=self.dir, publish=False)
        self.assertEqual(report["status"], "trained")
        self.assertFalse(report["suitable"])
        self.assertFalse(report["model_replaced"])
        self.assertEqual(report["active_model_version"], active)
        self.assertTrue((self.dir / REJECTED_DIR / report["model_version"] / MODEL_FILE).exists())
        self.assertIn("does not beat", report["suitability_reason"])
        state = load_ranker(self.dir, allow_synthetic=True)
        self.assertTrue(state.usable, "the previously validated model stays active")
        self.assertEqual(state.version, active)

        # A retrain with too little data also leaves the active model untouched.
        report = train_and_evaluate(other.head(5), provenance, model_dir=self.dir, publish=False)
        self.assertEqual(report["status"], "insufficient_data")
        self.assertEqual(report["active_model_version"], active)
        self.assertEqual(load_ranker(self.dir, allow_synthetic=True).version, active)

    def test_concurrent_training_in_one_directory_is_refused(self):
        with training_lock(self.dir):
            with self.assertRaises(TrainingInProgress):
                train_and_evaluate(self.frame, self.provenance, model_dir=self.dir, publish=False)
        self.assertFalse((self.dir / ".training.lock").exists())


class MeasuredModeTests(unittest.TestCase):
    """Training from the store on measured labels: eligibility, provenance, and prediction labelling."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = SampleStore(self.dir / "ml_dataset.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_measured_frame_trains_a_measured_model_and_synthetic_rows_are_ignored(self):
        synthetic, _ = make_synthetic_dataset(n_candidates=40, runs_per_candidate=2, seed=5)
        self.store.record_synthetic(synthetic, seed=5)
        self.store.upsert([build_sample(r) for r in measured_history(n_candidates=6, runs=4)])
        status = self.store.status()
        eligibility = measured_eligibility(status, 20, 5)
        self.assertTrue(eligibility["eligible"])
        self.assertEqual((eligibility["measured_valid_samples"], eligibility["measured_candidates"]), (24, 6))
        frame, provenance = self.store.training_frame()
        self.assertEqual(len(frame), 24, "synthetic rows are not in the measured frame")
        self.assertEqual(provenance["label_source"], LABEL_MEASURED)
        report = train_and_evaluate(frame, provenance, model_dir=self.dir / "models", publish=False)
        self.assertEqual(report["status"], "trained")
        self.assertEqual(report["sample_counts"], {"measured": 24, "synthetic": 0})
        state = load_ranker(self.dir / "models")  # synthetic not allowed: measured provenance is required
        self.assertEqual(state.label_source, LABEL_MEASURED)
        self.assertEqual(state.usable, report["suitable"])
        if state.usable:
            self.assertEqual(state.provenance_label, "measured-trained")


if __name__ == "__main__":
    unittest.main()
