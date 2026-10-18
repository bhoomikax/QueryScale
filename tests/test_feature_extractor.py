import csv
import tempfile
import unittest
from pathlib import Path

from backend.services.feature_extractor import extract_features
from helpers import patched_settings


class FeatureExtractorTests(unittest.TestCase):
    def test_extract_features_generates_aggregated_candidates(self):
        # Isolate from any local metadata snapshot: the schema comes from database/schema.sql.
        with tempfile.TemporaryDirectory() as tmpdir, patched_settings(
            metadata_source="schema", schema_metadata_path=str(Path(tmpdir) / "missing_snapshot.json")
        ):
            log_path = Path(tmpdir) / "query_logs.csv"
            output_path = Path(tmpdir) / "candidate_features.csv"

            with log_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow([
                    "query_id",
                    "query_template",
                    "timestamp",
                    "execution_time_ms",
                    "frequency",
                    "tables",
                    "where_columns",
                    "join_columns",
                    "order_columns",
                ])
                writer.writerow([
                    "Q001",
                    "SELECT * FROM Transactions WHERE account_id = 101 ORDER BY txn_date DESC;",
                    "2025-01-01T00:00:00Z",
                    "30.0",
                    "HIGH",
                    "Transactions",
                    "account_id",
                    "",
                    "txn_date",
                ])
                writer.writerow([
                    "Q002",
                    "SELECT * FROM Transactions WHERE txn_date BETWEEN \"2024-01-01\" AND \"2024-12-31\";",
                    "2025-01-01T00:01:00Z",
                    "40.0",
                    "HIGH",
                    "Transactions",
                    "txn_date",
                    "",
                    "",
                ])
                writer.writerow([
                    "Q004",
                    "SELECT c.name, a.account_id FROM Customers c JOIN Accounts a ON c.customer_id = a.customer_id WHERE c.customer_id = 42;",
                    "2025-01-01T00:02:00Z",
                    "55.0",
                    "MEDIUM",
                    "Customers;Accounts",
                    "customer_id",
                    "customer_id",
                    "",
                ])

            rows = extract_features(log_path, output_path)

            self.assertGreater(len(rows), 0)
            self.assertIn("candidate_id", rows[0])
            self.assertIn("frequency", rows[0])
            self.assertEqual(rows[0]["table"], "Transactions")
            self.assertGreaterEqual(rows[0]["average_execution_time"], 0.0)


if __name__ == "__main__":
    unittest.main()
