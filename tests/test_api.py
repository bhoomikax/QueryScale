import csv
import tempfile
import unittest
from pathlib import Path

from backend.app import app
from backend.routes.benchmark import _aggregate_results
from backend.routes.queries import read_query_logs
from backend.routes.recommendations import read_recommendations


class ApiTests(unittest.TestCase):
    def test_expected_api_routes_are_registered(self):
        paths = set(app.openapi()["paths"])

        self.assertIn("/api/queries", paths)
        self.assertIn("/api/queries/slow", paths)
        self.assertIn("/api/recommendations", paths)
        self.assertIn("/api/recommendations/{recommendation_id}", paths)
        self.assertIn("/api/benchmark/{recommendation_id}", paths)
        self.assertIn("/api/benchmark/results", paths)

    def test_query_and_recommendation_csv_data_is_parsed_for_api(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            query_path = Path(tmpdir) / "query_logs.csv"
            recommendation_path = Path(tmpdir) / "recommendations.csv"

            with query_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow([
                    "query_id", "query_template", "timestamp", "execution_time_ms",
                    "frequency", "tables", "where_columns", "join_columns", "order_columns",
                ])
                writer.writerow(["Q001", "SELECT 1", "2026-01-01T00:00:00Z", "420.5", "HIGH", "Transactions", "account_id", "", "txn_date"])

            with recommendation_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["candidate_id", "table", "columns", "score", "priority", "recommended_sql", "reason"])
                writer.writerow(["IDX_TRANSACTIONS_ACCOUNT_ID", "Transactions", "account_id", "87.5", "high", "CREATE INDEX", "Frequent filter usage"])

            queries = read_query_logs(query_path)
            recommendations = read_recommendations(recommendation_path)

            self.assertEqual(queries[0]["execution_time_ms"], 420.5)
            self.assertEqual(queries[0]["tables"], ["Transactions"])
            self.assertEqual(recommendations[0]["columns"], ["account_id"])
            self.assertEqual(recommendations[0]["score"], 87.5)

    def test_benchmark_aggregate_weights_query_groups_by_execution_count(self):
        summary = _aggregate_results([
            {"before_avg_ms": 10.0, "after_avg_ms": 20.0, "execution_count": 9},
            {"before_avg_ms": 100.0, "after_avg_ms": 50.0, "execution_count": 1},
        ])

        self.assertEqual(summary["before_ms"], 19.0)
        self.assertEqual(summary["after_ms"], 23.0)
        self.assertEqual(summary["improvement_percentage"], -21.053)
        self.assertEqual(summary["execution_count"], 10)


if __name__ == "__main__":
    unittest.main()