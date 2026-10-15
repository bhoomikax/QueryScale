import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from backend.services.jobs import COMPLETED, FAILED, JobConflict, JobManager
from backend.services.schema_metadata import (
    collect_mysql_metadata,
    load_metadata,
    load_snapshot,
    parse_schema_sql,
    save_snapshot,
)


class JobManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "jobs.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_inline_job_completes_and_failures_are_recorded(self):
        manager = JobManager(self.path, run_inline=True)
        job, created = manager.submit("analysis", "k", {"x": 1}, lambda params: {"echo": params["x"]})
        self.assertTrue(created)
        self.assertEqual(job["status"], COMPLETED)
        self.assertEqual(job["result"], {"echo": 1})

        def boom(params):
            raise ValueError("bad input")

        failed, _ = manager.submit("analysis", "k2", {}, boom)
        self.assertEqual(failed["status"], FAILED)
        self.assertEqual(failed["error"], {"code": "invalid_request", "message": "bad input"})
        self.assertEqual(manager.latest("analysis")["job_id"], failed["job_id"])

    def test_duplicate_requests_are_idempotent_and_conflicts_rejected(self):
        manager = JobManager(self.path, max_workers=1)
        release = threading.Event()
        job, created = manager.submit("benchmark", "IDX_A", {}, lambda params: release.wait(5) and {})
        try:
            again, created_again = manager.submit("benchmark", "IDX_A", {}, lambda params: {})
            self.assertTrue(created)
            self.assertFalse(created_again)
            self.assertEqual(again["job_id"], job["job_id"])
            with self.assertRaises(JobConflict):
                manager.submit("benchmark", "IDX_B", {}, lambda params: {})
        finally:
            release.set()
            manager._executor.shutdown(wait=True)
        self.assertEqual(manager.get(job["job_id"])["status"], COMPLETED)

    def test_interrupted_jobs_are_marked_failed_on_restart(self):
        manager = JobManager(self.path, max_workers=1)
        release = threading.Event()
        job, _ = manager.submit("analysis", "k", {}, lambda params: release.wait(5) and {})
        restarted = JobManager(self.path, run_inline=True)
        self.assertEqual(restarted.get(job["job_id"])["error"]["code"], "interrupted")
        release.set()
        manager._executor.shutdown(wait=True)


class FakeCursor:
    """Returns canned INFORMATION_SCHEMA rows; records every statement."""

    def __init__(self):
        self.statements = []
        self._result = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if sql.startswith("SELECT VERSION()"):
            self._result = [("8.0.40",)]
        elif "INFORMATION_SCHEMA.TABLES" in sql:
            self._result = [("loans", 5000, 400000, 100000)]
        elif "INFORMATION_SCHEMA.COLUMNS" in sql:
            self._result = [
                ("loans", "loan_id", "bigint", "bigint", "NO", None, 19, "auto_increment"),
                ("loans", "status", "varchar", "varchar(30)", "NO", 30, None, ""),
            ]
        elif "INFORMATION_SCHEMA.STATISTICS" in sql:
            self._result = [("loans", "PRIMARY", 0, 1, "loan_id", 4990), ("loans", "idx_loans_status", 1, 1, "status", 4)]
        elif "COUNT(DISTINCT" in sql:
            self._result = [(4,)]
        elif "COUNT(*)" in sql:
            self._result = [(5000,)]

    def fetchone(self):
        return self._result[0]

    def fetchall(self):
        return list(self._result)

    def close(self):
        pass


class SchemaMetadataTests(unittest.TestCase):
    def test_schema_sql_has_structure_but_no_statistics(self):
        schema = parse_schema_sql()
        transactions = schema.table("transactions")
        self.assertEqual(transactions.name, "Transactions")
        self.assertEqual(transactions.primary_key, ["txn_id"])
        self.assertIn(["txn_type", "txn_date"], [i.columns for i in transactions.indexes])
        self.assertIsNone(transactions.row_count)
        self.assertIsNone(transactions.column("account_id").cardinality)

    def test_live_collection_is_read_only_and_labels_sources(self):
        cursor = FakeCursor()
        connection = mock.MagicMock()
        connection.cursor.return_value = cursor
        metadata = collect_mysql_metadata(connection, "queryscale", exact_stats_max_rows=10000)
        loans = metadata.table("Loans")
        self.assertEqual(loans.name, "Loans", "lower-case server names map to schema spelling")
        self.assertEqual(loans.row_count, 5000)
        self.assertEqual(loans.row_count_source, "exact_count")
        self.assertEqual(loans.column("status").cardinality_source, "exact_count")
        for statement in cursor.statements:
            self.assertTrue(statement.lstrip().upper().startswith("SELECT"), statement)

    def test_large_tables_keep_estimates(self):
        connection = mock.MagicMock()
        connection.cursor.return_value = FakeCursor()
        metadata = collect_mysql_metadata(connection, "queryscale", exact_stats_max_rows=100)
        loans = metadata.table("Loans")
        self.assertEqual(loans.row_count_source, "information_schema_estimate")
        self.assertEqual(loans.column("status").cardinality_source, "index_statistics_estimate")

    def test_snapshot_round_trip_and_offline_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "meta.json"
            schema = parse_schema_sql()
            schema.table("Loans").row_count = 7
            save_snapshot(schema, path)
            restored = load_snapshot(path)
            self.assertEqual(restored.source, "snapshot")
            self.assertEqual(restored.table("Loans").row_count, 7)
            with mock.patch("mysql.connector.connect") as connect:
                offline = load_metadata(source="snapshot_or_schema", snapshot_path=Path(tmpdir) / "missing.json")
                connect.assert_not_called()
            self.assertEqual(offline.source, "schema")


if __name__ == "__main__":
    unittest.main()
