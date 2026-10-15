import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mysql.connector.errors as mysql_errors

from backend.services import benchmark
from backend.services.benchmark import (
    BenchmarkError,
    ReplayQuery,
    benchmark_lock,
    compare_timings,
    run_benchmark_detailed,
    summarize_workload,
    workload_signature,
)
from backend.services.index_manager import IndexApplication, classify_mysql_error
from backend.services.stats import latency_reduction_pct, speedup
from helpers import patched_settings, read_csv


class CalculationTests(unittest.TestCase):
    def test_p95_requires_enough_samples(self):
        small = compare_timings([1.0, 2.0, 3.0], [1.0, 1.0, 1.0])
        self.assertIsNone(small["before_p95_ms"])
        self.assertEqual(small["before_median_ms"], 2.0)
        large = compare_timings([float(i) for i in range(1, 21)], [1.0] * 20)
        self.assertEqual(large["before_p95_ms"], 19.05)

    def test_speedup_and_reduction_handle_invalid_values(self):
        self.assertEqual(speedup(10.0, 5.0), 2.0)
        self.assertIsNone(speedup(10.0, 0.0))
        self.assertIsNone(speedup(None, 5.0))
        self.assertEqual(latency_reduction_pct(10.0, 5.0), 50.0)
        self.assertEqual(latency_reduction_pct(10.0, 12.0), -20.0)
        self.assertIsNone(latency_reduction_pct(0.0, 1.0))

    def test_workload_benefit_counts_only_affected_queries(self):
        rows = [
            {"query_id": "A", "before_median_ms": 10.0, "after_median_ms": 2.0, "before_avg_ms": 10.0, "after_avg_ms": 2.0, "affected": True},
            {"query_id": "B", "before_median_ms": 5.0, "after_median_ms": 4.0, "before_avg_ms": 5.0, "after_avg_ms": 4.0, "affected": False},
        ]
        summary = summarize_workload(rows, {"A": 3, "B": 4})
        # (3 * (10 - 2)) / (3 * 10 + 4 * 5) = 24 / 50
        self.assertEqual(summary["workload_benefit"], 0.48)
        self.assertEqual(summary["affected_queries"], ["A"])
        self.assertEqual(summary["baseline_workload_median_ms"], 50.0)
        with self.assertRaises(ValueError):
            summarize_workload(rows, {})

    def test_workload_signature_is_stable(self):
        self.assertEqual(workload_signature({"Q1": "x"}, 10, 1, 1), workload_signature({"Q1": "x"}, 10, 1, 1))
        self.assertNotEqual(workload_signature({"Q1": "x"}, 10, 1, 1), workload_signature({"Q1": "x"}, 10, 2, 1))

    def test_mysql_errors_are_classified(self):
        self.assertEqual(classify_mysql_error(mysql_errors.ProgrammingError(errno=1049)), "test_database_missing")
        self.assertEqual(classify_mysql_error(mysql_errors.ProgrammingError(errno=1142)), "permission_denied")
        self.assertEqual(classify_mysql_error(mysql_errors.ProgrammingError(errno=1045)), "authentication_failed")
        self.assertEqual(classify_mysql_error(mysql_errors.DatabaseError(errno=3024)), "query_timeout")
        self.assertEqual(classify_mysql_error(mysql_errors.InterfaceError(errno=2003)), "connection_error")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.lock_patch = mock.patch.object(benchmark, "_lock_path", return_value=self.dir / "benchmark.lock")
        self.lock_patch.start()

    def tearDown(self):
        self.lock_patch.stop()
        self.tmp.cleanup()

    def test_primary_database_is_rejected_as_test_target(self):
        with patched_settings(db_name="queryscale", db_test_name="queryscale"), \
                mock.patch("mysql.connector.connect") as connect:
            with self.assertRaises(ValueError):
                from backend.services.index_manager import connect_to_test_database
                connect_to_test_database()
            connect.assert_not_called()

    def test_duplicate_runs_are_blocked(self):
        with benchmark_lock("IDX_A"):
            with self.assertRaises(BenchmarkError) as raised:
                with benchmark_lock("IDX_B"):
                    pass
        self.assertEqual(raised.exception.code, "benchmark_in_progress")
        with benchmark_lock("IDX_C"):
            pass  # released after the first run finished

    def test_missing_test_database_fails_cleanly_and_is_recorded(self):
        error = mysql_errors.ProgrammingError(msg="Unknown database", errno=1049)
        history = self.dir / "history.jsonl"
        with mock.patch("mysql.connector.connect", side_effect=error):
            with self.assertRaises(BenchmarkError) as raised:
                run_benchmark_detailed(
                    "IDX_TRANSACTIONS_ACCOUNT_ID_TXN_DATE", "Transactions", "account_id,txn_date",
                    iterations=5, output_path=self.dir / "results.csv", history_path=history,
                    candidate_features={"supporting_query_ids": "Q003"},
                )
        self.assertEqual(raised.exception.code, "test_database_missing")
        record = json.loads(history.read_text(encoding="utf-8").strip())
        self.assertEqual(record["status"], "FAILED")
        self.assertEqual(record["error"]["code"], "test_database_missing")
        self.assertIsNone(record["cleanup"], "no index was created, so nothing needed cleanup")
        self.assertFalse((self.dir / "results.csv").exists())

    def _patched_run(self, after_phase_error=None):
        plan = [ReplayQuery("Q003", "SELECT 1", ()), ReplayQuery("Q001", "SELECT 2", ())] * 2
        before = {"explain": {"Q003": [{"key": "idx_transactions_account_id"}], "Q001": [{"key": "idx_transactions_account_id"}]},
                  "rows_examined": {"Q003": 40.0, "Q001": 40.0},
                  "write_probe": {"insert_median_ms": 1.0, "update_median_ms": 1.0, "delete_median_ms": 1.0},
                  "timings": {"Q003": [10.0, 10.0], "Q001": [4.0, 4.0]}}
        after = {"explain": {"Q003": [{"key": "idx_transactions_account_id_txn_date"}], "Q001": [{"key": "idx_transactions_account_id"}]},
                 "rows_examined": {"Q003": 8.0, "Q001": 40.0},
                 "write_probe": {"insert_median_ms": 1.2, "update_median_ms": 1.1, "delete_median_ms": 1.0},
                 "timings": {"Q003": [5.0, 5.0], "Q001": [4.0, 4.0]}}
        phases = [before, after_phase_error or after]

        def measure(*args, **kwargs):
            value = phases.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        cursor = mock.MagicMock()
        cursor.fetchone.return_value = ("8.0.40",)
        connection = mock.MagicMock()
        connection.cursor.return_value = cursor
        application = IndexApplication("IDX_TRANSACTIONS_ACCOUNT_ID_TXN_DATE", "idx_transactions_account_id_txn_date",
                                       "Transactions", ("account_id", "txn_date"), "CREATE INDEX ...")
        patches = [
            mock.patch.object(benchmark, "reset_test_indexes", return_value=[]),
            mock.patch.object(benchmark, "_reference_indexes", return_value={}),
            mock.patch.object(benchmark, "connect_to_test_database", return_value=connection),
            mock.patch.object(benchmark, "_dataset_signature", return_value={"row_counts": {}, "hash": "D"}),
            mock.patch.object(benchmark, "_build_replay_plan", return_value=plan),
            mock.patch.object(benchmark, "_measure_phase", side_effect=measure),
            mock.patch.object(benchmark, "apply_index", return_value=application),
            mock.patch.object(benchmark, "measure_index_size_bytes", return_value=81920),
            mock.patch.object(benchmark, "drop_index", return_value=True),
        ]
        for patch in patches:
            patch.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        return benchmark.drop_index

    def test_completed_run_persists_label_and_cleans_up(self):
        drop = self._patched_run()
        history = self.dir / "history.jsonl"
        record = run_benchmark_detailed(
            "IDX_TRANSACTIONS_ACCOUNT_ID_TXN_DATE", "Transactions", "account_id,txn_date",
            iterations=4, output_path=self.dir / "results.csv", history_path=history,
            candidate_features={"supporting_query_ids": "Q003", "rule_score": "40"},
        )
        self.assertEqual(record["status"], "COMPLETED")
        self.assertEqual(record["summary"]["affected_queries"], ["Q003"])
        self.assertEqual(record["summary"]["affected_set_source"], "explain")
        # (2 * (10 - 5)) / (2 * 10 + 2 * 4) = 10 / 28
        self.assertAlmostEqual(record["summary"]["workload_benefit"], round(10 / 28, 6))
        self.assertEqual(record["index_size_bytes"], 81920)
        self.assertEqual(record["write_overhead"]["insert_overhead_pct"], 20.0)
        self.assertEqual(record["cleanup"]["status"], "dropped")
        drop.assert_called_once_with("idx_transactions_account_id_txn_date", "Transactions")
        rows = {row["query_id"]: row for row in read_csv(self.dir / "results.csv")}
        self.assertEqual(rows["Q003"]["after_rows_examined"], "8.0")
        self.assertEqual(rows["Q003"]["uses_candidate_index"], "True")
        self.assertEqual(json.loads(history.read_text(encoding="utf-8"))["status"], "COMPLETED")

    def test_index_is_dropped_when_post_index_phase_fails(self):
        error = mysql_errors.DatabaseError(msg="timeout", errno=3024)
        drop = self._patched_run(after_phase_error=error)
        with self.assertRaises(BenchmarkError) as raised:
            run_benchmark_detailed(
                "IDX_TRANSACTIONS_ACCOUNT_ID_TXN_DATE", "Transactions", "account_id,txn_date",
                iterations=4, output_path=self.dir / "results.csv", history_path=self.dir / "h.jsonl",
                candidate_features={},
            )
        self.assertEqual(raised.exception.code, "query_timeout")
        drop.assert_called_once()
        record = json.loads((self.dir / "h.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "FAILED")
        self.assertEqual(record["cleanup"]["status"], "dropped")

    def test_invalid_candidate_is_rejected_before_any_connection(self):
        with mock.patch("mysql.connector.connect") as connect:
            with self.assertRaises(ValueError):
                run_benchmark_detailed("IDX_USERS_ID", "Users", "id", output_path=self.dir / "r.csv", history_path=self.dir / "h.jsonl")
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
