import csv
import tempfile
import unittest
from pathlib import Path

from backend.services.pipeline import run_pipeline


class PipelineTests(unittest.TestCase):
    def test_run_pipeline_produces_recommendations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
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
                    "SELECT * FROM Loans WHERE status = 'ACTIVE' ORDER BY principal DESC;",
                    "2025-01-01T00:00:00Z",
                    "30.0",
                    "HIGH",
                    "Loans",
                    "status",
                    "",
                    "principal",
                ])

            rows = run_pipeline(log_path, feature_path, candidate_path, recommendation_path)

            self.assertGreater(len(rows), 0)
            self.assertIn("candidate_id", rows[0])
            self.assertIn("priority", rows[0])


if __name__ == "__main__":
    unittest.main()
