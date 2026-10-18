"""Regression tests for failure modes found in the 2026-10-18 audit.

Covered: an uninitialized (empty) live database, a stale snapshot without tables,
analysis jobs that must not look successful when they did no useful work,
benchmark jobs that must never be COMPLETED without measurements, and model
artifacts that must be refused when unsuitable or of unknown provenance.
No test here connects to MySQL.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mysql.connector.errors as mysql_errors

from backend.services import analysis, benchmark, schema_metadata
from backend.services.analysis import AnalysisError, run_analysis
from backend.services.benchmark import BenchmarkError, ReplayQuery, run_benchmark_detailed
from backend.services.index_manager import IndexApplication
from backend.services.jobs import FAILED, JobManager
from backend.services.ml.dataset import make_synthetic_dataset
from backend.services.ml.predictor import load_ranker
from backend.services.ml.trainer import METADATA_FILE, train_and_evaluate
from backend.services.schema_metadata import SchemaMetadata, load_metadata, load_snapshot
from helpers import log_rows, patched_settings, write_log


def _empty_live_metadata(*_args, **_kwargs) -> SchemaMetadata:
    return SchemaMetadata(database="queryscale", source="mysql", collected_at="2026-10-15T17:18:02Z", tables={})


class EmptyDatabaseMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.snapshot = Path(self.tmp.name) / "schema_metadata.json"
        self.settings_patch = patched_settings(schema_metadata_path=str(self.snapshot))
        self.settings_patch.__enter__()
        self.connect_patch = mock.patch.object(schema_metadata, "_connect_primary_read_only", return_value=mock.MagicMock())
        self.connect_patch.start()

    def tearDown(self):
        self.connect_patch.stop()
        self.settings_patch.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_auto_source_does_not_accept_or_save_an_empty_live_database(self):
        with mock.patch.object(schema_metadata, "collect_mysql_metadata", side_effect=_empty_live_metadata):
            metadata = load_metadata(source="auto")
        self.assertEqual(metadata.source, "schema", "falls back to database/schema.sql")
        self.assertGreater(len(metadata.tables), 0)
        self.assertTrue(any("has no tables" in w for w in metadata.warnings), metadata.warnings)
        self.assertFalse(self.snapshot.exists(), "an empty database must not be saved as a snapshot")

    def test_mysql_source_raises_for_an_empty_live_database(self):
        with mock.patch.object(schema_metadata, "collect_mysql_metadata", side_effect=_empty_live_metadata):
            with self.assertRaises(RuntimeError) as raised:
                load_metadata(source="mysql")
        self.assertIn("has no tables", str(raised.exception))

    def test_snapshot_without_tables_is_ignored(self):
        self.snapshot.write_text(json.dumps(_empty_live_metadata().to_dict()), encoding="utf-8")
        self.assertIsNone(load_snapshot(self.snapshot))
        with self.assertRaises(RuntimeError):
            load_metadata(source="snapshot")
        with mock.patch.object(schema_metadata, "collect_mysql_metadata", side_effect=mysql_errors.InterfaceError(errno=2003)):
            metadata = load_metadata(source="auto")
        self.assertEqual(metadata.source, "schema")


class AnalysisJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        overrides = {
            "query_log_path": str(self.dir / "query_logs.csv"),
            "query_feature_path": str(self.dir / "query_features.csv"),
            "feature_path": str(self.dir / "candidate_features.csv"),
            "candidate_path": str(self.dir / "index_candidates.csv"),
            "recommendation_path": str(self.dir / "recommendations.csv"),
            "schema_metadata_path": str(self.dir / "missing_snapshot.json"),
            "model_dir": str(self.dir / "models"),
            "metadata_source": "schema",
        }
        self.settings_patch = patched_settings(**overrides)
        self.settings_patch.__enter__()

    def tearDown(self):
        self.settings_patch.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_missing_or_empty_query_log_fails_the_job(self):
        with self.assertRaises(AnalysisError) as raised:
            run_analysis({"metadata_source": "schema"})
        self.assertEqual(raised.exception.code, "query_log_missing")
        write_log(self.dir / "query_logs.csv", [])
        with self.assertRaises(AnalysisError) as raised:
            run_analysis({"metadata_source": "schema"})
        self.assertEqual(raised.exception.code, "query_log_empty")

    def test_metadata_without_tables_fails_the_job(self):
        write_log(self.dir / "query_logs.csv", log_rows("Q001", "SELECT * FROM Loans WHERE status = %s", [5.0] * 3))
        with mock.patch.object(analysis, "load_metadata", side_effect=_empty_live_metadata):
            with self.assertRaises(AnalysisError) as raised:
                run_analysis({"metadata_source": "auto"})
        self.assertEqual(raised.exception.code, "schema_metadata_empty")

    def test_zero_candidates_completes_with_an_explicit_warning(self):
        write_log(self.dir / "query_logs.csv", log_rows("Q001", "SELECT * FROM NotATable WHERE x = %s", [5.0] * 3))
        summary = run_analysis({"metadata_source": "schema"})
        self.assertEqual(summary["count"], 0)
        self.assertEqual(summary["candidates_generated"], 0)
        self.assertTrue(any("No index candidates" in w for w in summary["warnings"]), summary["warnings"])

    def test_real_candidates_report_counts_and_no_warning(self):
        write_log(self.dir / "query_logs.csv", log_rows("Q009", "SELECT loan_id FROM Loans WHERE status = %s ORDER BY principal DESC", [8.0] * 6))
        summary = run_analysis({"metadata_source": "schema"})
        self.assertGreater(summary["candidates_generated"], 0)
        self.assertGreater(summary["count"], 0)
        self.assertEqual(summary["ranking_method"], "rule")
        # Only the schema.sql statistics note remains; no "no candidates" warning.
        self.assertFalse(any("candidates" in w for w in summary["warnings"]), summary["warnings"])

    def test_job_manager_records_analysis_error_code(self):
        manager = JobManager(self.dir / "jobs.sqlite3", run_inline=True)
        job, _ = manager.submit("analysis", "k", {"metadata_source": "schema"}, run_analysis)
        self.assertEqual(job["status"], FAILED)
        self.assertEqual(job["error"]["code"], "query_log_missing")


class BenchmarkJobTests(unittest.TestCase):
    """A benchmark run is COMPLETED only when both phases produced timings."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.lock_patch = mock.patch.object(benchmark, "_lock_path", return_value=self.dir / "benchmark.lock")
        self.lock_patch.start()

    def tearDown(self):
        self.lock_patch.stop()
        self.tmp.cleanup()

    def _patch_run(self, before, after):
        phases = [before, after]
        cursor = mock.MagicMock()
        cursor.fetchone.return_value = ("8.4.11",)
        connection = mock.MagicMock()
        connection.cursor.return_value = cursor
        application = IndexApplication("IDX_LOANS_STATUS", "idx_loans_status", "Loans", ("status",), "CREATE INDEX ...")
        patches = [
            mock.patch.object(benchmark, "reset_test_indexes", return_value=[]),
            mock.patch.object(benchmark, "_reference_indexes", return_value={}),
            mock.patch.object(benchmark, "connect_to_test_database", return_value=connection),
            mock.patch.object(benchmark, "_dataset_signature", return_value={"row_counts": {}, "hash": "D"}),
            mock.patch.object(benchmark, "_build_replay_plan", return_value=[ReplayQuery("Q009", "SELECT 1", ())]),
            mock.patch.object(benchmark, "_measure_phase", side_effect=lambda *a, **k: phases.pop(0)),
            mock.patch.object(benchmark, "apply_index", return_value=application),
            mock.patch.object(benchmark, "measure_index_size_bytes", return_value=None),
            mock.patch.object(benchmark, "drop_index", return_value=True),
        ]
        for patch in patches:
            patch.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def test_run_without_measurements_is_failed_not_completed(self):
        empty = {"explain": {}, "rows_examined": {}, "write_probe": {}, "timings": {}}
        self._patch_run(empty, empty)
        history = self.dir / "history.jsonl"
        with self.assertRaises((BenchmarkError, ValueError)):
            run_benchmark_detailed("IDX_LOANS_STATUS", "Loans", "status", iterations=1,
                                   output_path=self.dir / "results.csv", history_path=history, candidate_features={})
        record = json.loads(history.read_text(encoding="utf-8").strip())
        self.assertEqual(record["status"], "FAILED")
        self.assertNotIn("summary", record)
        self.assertEqual(record["cleanup"]["status"], "dropped", "the index created before the failure is removed")
        self.assertFalse((self.dir / "results.csv").exists())

    def test_job_manager_never_completes_a_failed_benchmark(self):
        def failing_handler(_params):
            raise BenchmarkError("test_database_missing", "Test database 'queryscale_test' does not exist")

        manager = JobManager(self.dir / "jobs.sqlite3", run_inline=True)
        job, _ = manager.submit("benchmark", "k", {}, failing_handler)
        self.assertEqual(job["status"], FAILED)
        self.assertEqual(job["error"]["code"], "test_database_missing")
        self.assertIsNone(job["result"])


class ModelArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        frame, provenance = make_synthetic_dataset(n_candidates=30, runs_per_candidate=2, seed=3)
        train_and_evaluate(frame, provenance, model_dir=self.dir, publish=False)
        self.metadata_path = self.dir / METADATA_FILE

    def tearDown(self):
        self.tmp.cleanup()

    def _rewrite(self, **changes):
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        metadata.update(changes)
        self.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    def test_unsuitable_model_is_refused_even_when_synthetic_is_allowed(self):
        self._rewrite(suitable=False, suitability_reason="Held-out MAE does not beat the mean baseline.")
        state = load_ranker(self.dir, allow_synthetic=True)
        self.assertTrue(state.available)
        self.assertFalse(state.usable)
        self.assertIn("baseline", state.reason)

    def test_unknown_provenance_is_refused(self):
        self._rewrite(label_source="measured_benchmark_v0")
        state = load_ranker(self.dir, allow_synthetic=True)
        self.assertFalse(state.usable)
        self.assertIn("provenance", state.reason)
        self._rewrite(label_source=None)
        self.assertFalse(load_ranker(self.dir, allow_synthetic=True).usable)

    def test_retraining_archives_the_previous_artifacts_instead_of_overwriting(self):
        from backend.services.ml.trainer import ARCHIVE_DIR, EVALUATION_FILE, MODEL_FILE

        first_version = json.loads(self.metadata_path.read_text(encoding="utf-8"))["model_version"]
        frame, provenance = make_synthetic_dataset(n_candidates=30, runs_per_candidate=2, seed=4)
        report = train_and_evaluate(frame, provenance, model_dir=self.dir, publish=False)
        archive = self.dir / ARCHIVE_DIR / first_version.replace(":", "").replace("-", "")
        self.assertEqual(report["archived_previous"], str(archive))
        for name in (MODEL_FILE, METADATA_FILE, EVALUATION_FILE):
            self.assertTrue((archive / name).exists(), name)
        self.assertNotEqual(report["model_version"], first_version)
        self.assertEqual(load_ranker(self.dir, allow_synthetic=True).version, report["model_version"])

    def test_synthetic_model_cannot_be_relabelled_as_measured_without_retraining(self):
        """Changing label_source alone does not change the checksum, so the model would load.
        The provenance block still says synthetic; the API exposes it so a reviewer can see it."""
        self._rewrite(label_source="measured_benchmark")
        state = load_ranker(self.dir)
        self.assertTrue(state.usable)
        self.assertEqual(state.metadata["provenance"]["label_source"], "synthetic_demo")
        self.assertIn("SYNTHETIC", state.metadata["provenance"]["target_definition"])


if __name__ == "__main__":
    unittest.main()
