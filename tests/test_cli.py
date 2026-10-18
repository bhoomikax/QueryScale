import csv
import tempfile
import unittest
from pathlib import Path

from backend.services.cli import main
from helpers import patched_settings


class CLIIntegrationTests(unittest.TestCase):
    def test_main_generates_recommendations_for_log_file(self):
        # Isolate from any local metadata snapshot and model directory.
        with tempfile.TemporaryDirectory() as tmpdir, patched_settings(
            schema_metadata_path=str(Path(tmpdir) / "missing_snapshot.json"), model_dir=str(Path(tmpdir) / "models")
        ):
            log_path = Path(tmpdir) / "query_logs.csv"
            feature_path = Path(tmpdir) / "candidate_features.csv"
            candidate_path = Path(tmpdir) / "index_candidates.csv"
            recommendation_path = Path(tmpdir) / "recommendations.csv"

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
                    "SELECT * FROM Loans WHERE loan_status = 'ACTIVE' ORDER BY branch_id DESC;",
                    "2025-01-01T00:00:00Z",
                    "40.0",
                    "HIGH",
                    "Loans",
                    "loan_status",
                    "",
                    "branch_id",
                ])

            exit_code = main([
                "--log-path", str(log_path),
                "--feature-path", str(feature_path),
                "--candidate-path", str(candidate_path),
                "--recommendation-path", str(recommendation_path),
            ])

            self.assertEqual(exit_code, 0)
            self.assertTrue(recommendation_path.exists())
            self.assertGreater(recommendation_path.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
