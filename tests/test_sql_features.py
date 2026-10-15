import tempfile
import unittest
from pathlib import Path

from backend.services.feature_extractor import extract_query_features
from backend.services.query_analyzer import analyze_query, normalize_placeholders
from helpers import log_rows


class SqlAnalyzerTests(unittest.TestCase):
    def test_composite_predicates_are_classified(self):
        result = analyze_query(
            "SELECT txn_id FROM Transactions WHERE txn_type = %s AND amount > %s AND account_id IN (%s, %s)"
        )
        kinds = {p["column"]: p["kind"] for p in result["predicates"]}
        self.assertEqual(result["parse_status"], "ok")
        self.assertEqual(result["query_type"], "SELECT")
        self.assertEqual(kinds, {"txn_type": "equality", "amount": "range", "account_id": "in"})
        self.assertEqual(result["projection"], {"star": False, "columns": [{"table": "Transactions", "column": "txn_id"}]})

    def test_join_order_and_alias_resolution(self):
        result = analyze_query(
            "SELECT c.name FROM Customers c JOIN Accounts a ON c.customer_id = a.customer_id "
            "WHERE a.branch_id = ? ORDER BY c.name DESC LIMIT 10"
        )
        self.assertEqual(result["tables"], ["Customers", "Accounts"])
        self.assertEqual(result["join_pairs"][0]["left_table"], "Customers")
        self.assertEqual(result["join_pairs"][0]["right_table"], "Accounts")
        self.assertEqual(result["predicates"][0], {"table": "Accounts", "column": "branch_id", "kind": "equality", "source": "where"})
        self.assertEqual(result["order_by"][0]["direction"], "DESC")
        self.assertTrue(result["has_limit"])

    def test_group_by_function_and_alias_are_not_indexable(self):
        result = analyze_query(
            "SELECT DATE_FORMAT(txn_date, '%Y-%m') AS month, SUM(amount) FROM Transactions "
            "WHERE txn_date BETWEEN %s AND %s GROUP BY DATE_FORMAT(txn_date, '%Y-%m') ORDER BY month"
        )
        self.assertEqual(result["group_columns"], ["txn_date"])
        self.assertFalse(result["group_by"][0]["indexable"])
        self.assertFalse(result["order_by"][0]["indexable"])
        self.assertEqual(result["predicates"][0]["kind"], "range")

    def test_function_wrapped_and_or_predicates_are_marked(self):
        wrapped = analyze_query("SELECT * FROM Transactions WHERE YEAR(txn_date) = 2024")
        self.assertEqual(wrapped["predicates"][0]["kind"], "function")
        disjunctive = analyze_query("SELECT * FROM Loans WHERE status = 'A' OR customer_id = 3")
        self.assertEqual({p["kind"] for p in disjunctive["predicates"]}, {"disjunctive"})

    def test_update_statement(self):
        result = analyze_query("UPDATE Accounts SET balance = balance + %s WHERE account_id = %s")
        self.assertEqual(result["query_type"], "UPDATE")
        self.assertEqual(result["where_columns"], ["account_id"])

    def test_placeholders_inside_literals_are_preserved(self):
        self.assertEqual(normalize_placeholders("SELECT '%s' FROM t WHERE a = %s AND b = %(x)s"), "SELECT '%s' FROM t WHERE a = ? AND b = ?")

    def test_malformed_sql_does_not_raise(self):
        broken = analyze_query("SELEC * FROMM WHERE")
        self.assertEqual(broken["parse_status"], "error")
        self.assertEqual(broken["tables"], [])
        self.assertTrue(broken["parse_error"])
        empty = analyze_query("")
        self.assertEqual(empty["parse_status"], "error")


class QueryFeatureTests(unittest.TestCase):
    def test_latency_statistics_and_missing_metrics(self):
        rows = log_rows("Q1", "SELECT * FROM Loans WHERE customer_id = %s", [1.0, 2.0, 3.0, 10.0])
        rows += log_rows("Q2", "SELECT * FROM Accounts WHERE account_id = %s", [5.0] * 25, rows_examined=0, rows_returned=1)
        rows += log_rows("Q3", "UPDATE Accounts SET balance = %s WHERE account_id = %s", [2.0] * 5)
        features, workload = extract_query_features([dict(zip(
            ["query_id", "query_template", "timestamp", "execution_time_ms", "frequency", "tables",
             "where_columns", "join_columns", "order_columns", "rows_returned", "rows_examined"], row)) for row in rows])
        by_id = {f["query_id"]: f for f in features}

        self.assertEqual(by_id["Q1"]["execution_count"], 4)
        self.assertEqual(by_id["Q1"]["median_latency_ms"], 2.5)
        self.assertIsNone(by_id["Q1"]["p95_latency_ms"], "P95 requires at least 20 samples")
        self.assertIsNone(by_id["Q1"]["rows_examined_avg"], "missing metric must stay unavailable")
        self.assertEqual(by_id["Q2"]["rows_examined_avg"], 0.0, "a measured zero must stay zero")
        self.assertIsNotNone(by_id["Q2"]["p95_latency_ms"])
        self.assertEqual(workload["table_activity"]["Accounts"], {"reads": 25, "writes": 5, "write_ratio": round(5 / 30, 6)})
        self.assertAlmostEqual(sum(f["workload_share"] for f in features), 1.0, places=5)

    def test_malformed_query_is_reported_not_raised(self):
        features, workload = extract_query_features([
            {"query_id": "BAD", "query_template": "SELEC nonsense", "execution_time_ms": "3"},
        ])
        self.assertEqual(features[0]["parse_status"], "error")
        self.assertEqual(workload["unparsed_queries"], ["BAD"])

    def test_unparseable_latency_is_ignored(self):
        features, _ = extract_query_features([
            {"query_id": "Q", "query_template": "SELECT * FROM Loans WHERE status = %s", "execution_time_ms": ""},
            {"query_id": "Q", "query_template": "SELECT * FROM Loans WHERE status = %s", "execution_time_ms": "4"},
        ])
        self.assertEqual(features[0]["execution_count"], 1)


if __name__ == "__main__":
    unittest.main()
