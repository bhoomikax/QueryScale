import csv
import tempfile
import unittest
from pathlib import Path

from backend.services.recommender import generate_recommendations


class RecommenderTests(unittest.TestCase):
    def test_generate_recommendations_prioritizes_high_score_indexes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_path = Path(tmpdir) / "index_candidates.csv"
            output_path = Path(tmpdir) / "recommendations.csv"

            with candidate_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow([
                    "candidate_id",
                    "table",
                    "columns",
                    "score",
                    "recommended_action",
                    "reason",
                ])
                writer.writerow([
                    "IDX_TRANSACTIONS_account_id",
                    "Transactions",
                    "account_id",
                    "65.5",
                    "CREATE INDEX idx_transactions_account_id ON Transactions (account_id);",
                    "High query frequency and filter usage indicate this index would reduce expensive scans.",
                ])
                writer.writerow([
                    "IDX_CUSTOMERS_customer_id",
                    "Customers",
                    "customer_id",
                    "2.0",
                    "CREATE INDEX idx_customers_customer_id ON Customers (customer_id);",
                    "Low value recommendation.",
                ])

            # Rule-only: this test is about the heuristic ranking, not about any locally trained model.
            rows = generate_recommendations(candidate_path, output_path, use_ml=False)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["candidate_id"], "IDX_TRANSACTIONS_account_id")
            self.assertEqual(rows[0]["priority"], "high")
            self.assertIn("CREATE INDEX", rows[0]["recommended_sql"])


if __name__ == "__main__":
    unittest.main()
