import json
import tempfile
import unittest
from pathlib import Path

from backend.services.candidate_generator import build_candidates, coverage, generate_candidates
from backend.services.feature_extractor import extract_features, extract_query_features
from backend.services.rule_scorer import DEFAULT_WEIGHTS, load_rule_weights, priority_for, score_candidate
from backend.services.schema_metadata import parse_schema_sql
from helpers import log_rows, read_csv, write_log


FIELDS = ["query_id", "query_template", "timestamp", "execution_time_ms", "frequency", "tables",
          "where_columns", "join_columns", "order_columns", "rows_returned", "rows_examined"]


def schema_with_stats():
    schema = parse_schema_sql()
    sizes = {"Branches": 50, "Customers": 10000, "Accounts": 15000, "Transactions": 100000, "Loans": 5000}
    for name, table in schema.tables.items():
        table.row_count = sizes[name]
        table.row_count_source = "exact_count"
    transactions = schema.table("Transactions")
    transactions.column("account_id").cardinality = 15000
    transactions.column("txn_type").cardinality = 5
    transactions.column("amount").cardinality = 99000
    for column in transactions.columns.values():
        column.cardinality_source = column.cardinality_source or "exact_count"
    return schema


def candidates_for(*queries, schema=None, **limits):
    rows = []
    for query_id, sql, latencies in queries:
        rows += [dict(zip(FIELDS, row)) for row in log_rows(query_id, sql, latencies)]
    features, workload = extract_query_features(rows)
    return {c["candidate_id"]: c for c in build_candidates(features, workload, schema or schema_with_stats(), **limits)}


class CandidateGenerationTests(unittest.TestCase):
    def test_equality_then_order_by_composite(self):
        candidates = candidates_for(("Q3", "SELECT * FROM Transactions WHERE account_id = %s ORDER BY txn_date DESC", [3.0] * 5))
        composite = candidates["IDX_TRANSACTIONS_ACCOUNT_ID_TXN_DATE"]
        self.assertEqual(composite["columns"], "account_id,txn_date")
        self.assertTrue(composite["covers_order_by"])
        self.assertFalse(composite["existing_index"])
        self.assertEqual(composite["existing_prefix_index"], "idx_transactions_account_id")
        self.assertEqual(composite["existing_prefix_length"], 1)
        self.assertTrue(candidates["IDX_TRANSACTIONS_ACCOUNT_ID"]["existing_index"], "single column is already indexed")

    def test_equality_before_range_ordering(self):
        candidates = candidates_for(("Q11", "SELECT txn_id FROM Transactions WHERE amount > %s AND txn_type = %s", [9.0] * 3))
        self.assertIn("IDX_TRANSACTIONS_TXN_TYPE_AMOUNT", candidates)
        self.assertNotIn("IDX_TRANSACTIONS_AMOUNT_TXN_TYPE", candidates)
        candidate = candidates["IDX_TRANSACTIONS_TXN_TYPE_AMOUNT"]
        self.assertEqual(candidate["equality_prefix_length"], 1)
        self.assertTrue(candidate["has_range_column"])

    def test_low_cardinality_column_is_kept_as_candidate(self):
        candidates = candidates_for(("Q", "SELECT txn_id FROM Transactions WHERE txn_type = %s", [2.0] * 4))
        self.assertIn("IDX_TRANSACTIONS_TXN_TYPE", candidates)
        self.assertIn("low cardinality", json.dumps(candidates["IDX_TRANSACTIONS_TXN_TYPE"]["drawbacks"]))

    def test_primary_key_and_innodb_pk_suffix_coverage(self):
        schema = schema_with_stats()
        table = schema.table("Transactions")
        self.assertEqual(coverage(table, ["txn_id"])[0], "PRIMARY")
        self.assertEqual(coverage(table, ["account_id", "txn_id"])[0], "idx_transactions_account_id")
        self.assertIsNone(coverage(table, ["account_id", "txn_date"])[0])
        candidates = candidates_for(("Q", "SELECT * FROM Transactions WHERE txn_id = %s", [1.0]))
        self.assertEqual(candidates, {}, "full primary-key lookups produce no candidates")

    def test_join_and_covering_candidates(self):
        candidates = candidates_for(
            ("Q4", "SELECT c.name, a.account_id FROM Customers c JOIN Accounts a ON c.customer_id = a.customer_id WHERE c.branch_id = %s", [4.0] * 2),
            ("Q12", "SELECT account_id, customer_id FROM Accounts WHERE balance BETWEEN %s AND %s", [6.0] * 2),
        )
        self.assertTrue(candidates["IDX_ACCOUNTS_CUSTOMER_ID"]["existing_index"])
        self.assertIn("join", candidates["IDX_ACCOUNTS_CUSTOMER_ID"]["generation_rules"])
        self.assertTrue(candidates["IDX_ACCOUNTS_BALANCE_CUSTOMER_ID"]["is_covering"])

    def test_invalid_columns_and_tables_are_rejected(self):
        candidates = candidates_for(
            ("Q", "SELECT * FROM Loans WHERE loan_status = 'A' ORDER BY branch_id", [1.0]),
            ("Q2", "SELECT * FROM Users WHERE id = 1", [1.0]),
        )
        self.assertEqual(candidates, {})

    def test_candidates_are_deduplicated_with_merged_evidence(self):
        candidates = candidates_for(
            ("Q1", "SELECT * FROM Loans WHERE status = %s AND loan_type = %s", [2.0] * 3),
            ("Q2", "SELECT * FROM Loans WHERE loan_type = %s AND status = %s", [2.0] * 2),
        )
        loan_ids = [cid for cid in candidates if cid.startswith("IDX_LOANS_") and "LOAN_TYPE" in cid and "STATUS" in cid]
        self.assertEqual(len(loan_ids), 1, loan_ids)
        self.assertEqual(candidates[loan_ids[0]]["supporting_observations"], 5)
        self.assertEqual(candidates[loan_ids[0]]["supporting_query_ids"], ["Q1", "Q2"])

    def test_candidate_count_is_bounded(self):
        queries = [
            (f"Q{i}", f"SELECT * FROM Transactions WHERE {col} = %s", [float(i + 1)])
            for i, col in enumerate(["amount", "txn_type", "account_id", "txn_date"])
        ]
        candidates = candidates_for(*queries, max_candidates=1)
        actionable = [c for c in candidates.values() if not c["existing_index"]]
        self.assertEqual(len(actionable), 1)

    def test_storage_is_estimated_only_when_row_count_known(self):
        query = ("Q", "SELECT * FROM Loans WHERE status = %s ORDER BY principal DESC", [2.0])
        with_stats = candidates_for(query)["IDX_LOANS_STATUS_PRINCIPAL"]
        without_stats = candidates_for(query, schema=parse_schema_sql())["IDX_LOANS_STATUS_PRINCIPAL"]
        self.assertGreater(with_stats["estimated_index_bytes"], 0)
        self.assertEqual(with_stats["storage_source"], "estimated")
        self.assertIsNone(without_stats["estimated_index_bytes"])
        self.assertIsNone(without_stats["table_size"])
        self.assertEqual(without_stats["storage_source"], "unavailable")


class RuleScorerTests(unittest.TestCase):
    def base(self, **overrides):
        row = {
            "table": "Transactions", "columns": "account_id,txn_date", "column_count": 2,
            "time_share": 0.4, "workload_share": 0.3, "supporting_observations": 30,
            "equality_prefix_length": 1, "covers_order_by": "true", "table_size": 100000,
            "leading_column_cardinality": 15000, "write_ratio": 0.0, "estimated_index_bytes": 2_000_000,
            "existing_index": "false", "supporting_query_ids": "Q3",
        }
        row.update(overrides)
        return row

    def test_covered_candidate_scores_zero(self):
        result = score_candidate(self.base(existing_index="true", covered_by="idx_x"))
        self.assertEqual(result["rule_score"], 0.0)
        self.assertIn("idx_x", result["reason"])

    def test_score_is_deterministic_bounded_and_labelled_heuristic(self):
        first, second = score_candidate(self.base()), score_candidate(self.base())
        self.assertEqual(first, second)
        self.assertTrue(0 < first["rule_score"] <= 100)
        self.assertIn("not a measured speedup", first["reason"])
        self.assertTrue(any("ORDER BY" in e for e in first["positive_evidence"]))
        self.assertTrue(any("estimate" in c for c in first["costs"]))

    def test_writes_and_existing_prefix_reduce_score(self):
        base = score_candidate(self.base())["rule_score"]
        self.assertLess(score_candidate(self.base(write_ratio=0.5))["rule_score"], base)
        self.assertLess(score_candidate(self.base(existing_prefix_length=1, existing_prefix_index="idx_a"))["rule_score"], base)

    def test_unavailable_metrics_are_listed_not_zeroed(self):
        result = score_candidate(self.base(table_size="", leading_column_cardinality="", write_ratio="", estimated_index_bytes=""))
        self.assertIn("table row count", result["unavailable"])
        self.assertIn("leading column cardinality", result["unavailable"])
        self.assertIn("read/write ratio", result["unavailable"])
        self.assertEqual(result["components"]["table_size"], DEFAULT_WEIGHTS.neutral_unknown)

    def test_weights_are_configurable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "weights.json"
            path.write_text(json.dumps({"high_threshold": 90, "selectivity": 0.0}), encoding="utf-8")
            weights = load_rule_weights(path)
            self.assertEqual(priority_for(80, weights), "medium")
            path.write_text(json.dumps({"unknown": 1}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_rule_weights(path)

    def test_generate_candidates_writes_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log = write_log(Path(tmpdir) / "log.csv", log_rows("Q3", "SELECT * FROM Transactions WHERE account_id = %s ORDER BY txn_date DESC", [3.0] * 4))
            features = Path(tmpdir) / "features.csv"
            extract_features(log, features, metadata=schema_with_stats())
            scored = generate_candidates(features, Path(tmpdir) / "candidates.csv")
            self.assertEqual([r["candidate_id"] for r in scored], ["IDX_TRANSACTIONS_ACCOUNT_ID_TXN_DATE"])
            written = read_csv(Path(tmpdir) / "candidates.csv")[0]
            self.assertTrue(json.loads(written["positive_evidence"]))
            self.assertIn("CREATE INDEX `idx_transactions_account_id_txn_date`", written["recommended_action"])


if __name__ == "__main__":
    unittest.main()
