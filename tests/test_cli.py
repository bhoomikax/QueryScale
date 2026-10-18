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
                # Loans has columns loan_id, customer_id, loan_type, principal, status (database/schema.sql).
                for i in range(6):
                    writer.writerow([
                        "Q009",
                        "SELECT loan_id FROM Loans WHERE status = 'ACTIVE' ORDER BY principal DESC LIMIT 50;",
                        f"2025-01-01T00:00:{i:02d}Z",
                        "40.0",
                        "HIGH",
                        "Loans",
                        "status",
                        "",
                        "principal",
                    ])

            exit_code = main([
                "--log-path", str(log_path),
                "--feature-path", str(feature_path),
                "--candidate-path", str(candidate_path),
                "--recommendation-path", str(recommendation_path),
                "--metadata-source", "schema",
                "--no-ml",
            ])

            self.assertEqual(exit_code, 0)
            for path in (feature_path, candidate_path, recommendation_path):
                self.assertTrue(path.exists(), path)

            with candidate_path.open("r", newline="", encoding="utf-8") as handle:
                candidates = list(csv.DictReader(handle))
            self.assertGreater(len(candidates), 0, "a valid query on real schema columns must yield candidates")
            for required in ("candidate_id", "table", "columns", "rule_score", "priority", "recommended_action", "reason"):
                self.assertIn(required, candidates[0])
            self.assertTrue(all(row["table"] == "Loans" for row in candidates))
            self.assertTrue(all(row["candidate_id"].startswith("IDX_LOANS_") for row in candidates))
            self.assertTrue(any("status" in row["columns"] for row in candidates), "status is the equality column")
            self.assertTrue(all(row["recommended_action"].startswith("CREATE INDEX") for row in candidates))

            with recommendation_path.open("r", newline="", encoding="utf-8") as handle:
                recommendations = list(csv.DictReader(handle))
            self.assertGreater(len(recommendations), 0)
            self.assertEqual(recommendations[0]["rank"], "1")
            self.assertEqual(recommendations[0]["ranking_method"], "rule")
            self.assertIn(recommendations[0]["priority"], {"high", "medium"})
            self.assertIn("status", recommendations[0]["columns"])


if __name__ == "__main__":
    unittest.main()
