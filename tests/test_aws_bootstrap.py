"""Unit tests for the AWS database bootstrap and validation scripts (no MySQL needed)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mysql.connector.errors import Error as MySQLError

from backend.services.test_database import EXPECTED_TABLES
from scripts import aws_bootstrap, aws_db_validate
from scripts.aws_bootstrap import (
    APP_GRANTS,
    BENCHMARK_GRANTS,
    JOBS_GRANTS,
    DatabaseLayout,
    DatabaseUser,
    SchemaStatements,
    admin_username_is_not_an_app_user,
    build_users,
    decide_schema_action,
    decide_seed_action,
    decide_test_db_action,
    load_schema_statements,
    user_statements,
)
from scripts.aws_db_validate import (
    Check,
    classify_probe,
    compare_row_counts,
    forbidden_queries,
    grants_match,
    leftover_indexes,
    normalize_grant,
)


ROOT = Path(__file__).resolve().parents[1]


def env(**overrides: str) -> dict[str, str]:
    base = {
        "DB_APP_USER": "queryscale_app",
        "DB_APP_PASSWORD": "app-secret",
        "DB_BENCHMARK_USER": "queryscale_benchmark",
        "DB_BENCHMARK_PASSWORD": "bench-secret",
        "DB_JOBS_USER": "queryscale_jobs",
        "DB_JOBS_PASSWORD": "jobs-secret",
    }
    base.update(overrides)
    return base


LAYOUT = DatabaseLayout("queryscale", "queryscale_test", "queryscale_jobs")


class SchemaFileTests(unittest.TestCase):
    def test_repository_schema_is_split_into_creates_and_drops(self):
        statements = load_schema_statements(ROOT / "database" / "schema.sql")
        self.assertEqual(len(statements.creates), len(EXPECTED_TABLES))
        self.assertEqual(len(statements.drops), len(EXPECTED_TABLES))
        self.assertEqual(len(statements.skipped), 2)  # CREATE DATABASE + USE
        self.assertTrue(all(s.upper().startswith("CREATE TABLE") for s in statements.creates))
        self.assertTrue(all(s.upper().startswith("DROP TABLE") for s in statements.drops))
        self.assertTrue(any("`queryscale`" in s for s in statements.skipped))

    def test_unexpected_statement_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schema.sql"
            path.write_text("CREATE TABLE `A` (id INT); DELETE FROM `A`;", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unexpected statement"):
                load_schema_statements(path)


class DecisionTests(unittest.TestCase):
    def test_schema_created_only_when_no_tables_exist(self):
        self.assertEqual(decide_schema_action([], reinitialize=False), "create")
        self.assertEqual(decide_schema_action(EXPECTED_TABLES, reinitialize=False), "skip")
        self.assertEqual(decide_schema_action(EXPECTED_TABLES, reinitialize=True), "recreate")

    def test_partial_schema_aborts_instead_of_guessing(self):
        with self.assertRaisesRegex(RuntimeError, "partially initialised"):
            decide_schema_action(EXPECTED_TABLES[:2], reinitialize=False)

    def test_seed_skipped_when_any_table_has_rows(self):
        self.assertEqual(decide_seed_action({t: 0 for t in EXPECTED_TABLES}, reseed=False), "seed")
        self.assertEqual(decide_seed_action({"Branches": 3}, reseed=False), "skip")
        self.assertEqual(decide_seed_action({"Branches": 3}, reseed=True), "reseed")

    def test_test_db_rebuilt_only_when_missing_or_incomplete(self):
        self.assertEqual(decide_test_db_action(EXPECTED_TABLES, rebuild=False), "skip")
        self.assertEqual(decide_test_db_action([], rebuild=False), "rebuild")
        self.assertEqual(decide_test_db_action(EXPECTED_TABLES[:1], rebuild=False), "rebuild")
        self.assertEqual(decide_test_db_action(EXPECTED_TABLES, rebuild=True), "rebuild")


class LayoutAndUserTests(unittest.TestCase):
    def test_three_databases_must_be_distinct(self):
        with self.assertRaisesRegex(ValueError, "three different databases"):
            DatabaseLayout("queryscale", "queryscale", "queryscale_jobs").validate()
        with self.assertRaisesRegex(ValueError, "three different databases"):
            DatabaseLayout("queryscale", "queryscale_test", "QUERYSCALE_TEST").validate()
        self.assertIs(LAYOUT.validate(), LAYOUT)

    def test_invalid_database_identifier_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid database identifier"):
            DatabaseLayout("queryscale; DROP DATABASE x", "t", "j").validate()

    def test_users_are_built_from_environment_with_least_privilege_grants(self):
        users = build_users(env(), LAYOUT)
        by_name = {u.username: u for u in users}
        self.assertEqual(by_name["queryscale_app"].grants, APP_GRANTS)
        self.assertEqual(by_name["queryscale_app"].database, "queryscale")
        self.assertEqual(by_name["queryscale_benchmark"].grants, BENCHMARK_GRANTS)
        self.assertEqual(by_name["queryscale_benchmark"].database, "queryscale_test")
        self.assertEqual(by_name["queryscale_jobs"].grants, JOBS_GRANTS)
        self.assertEqual(by_name["queryscale_jobs"].database, "queryscale_jobs")
        self.assertNotIn("DROP", JOBS_GRANTS)
        self.assertNotIn("INSERT", APP_GRANTS)
        # The benchmark replays rolled-back writes and runs ANALYZE TABLE on the disposable
        # test database, so it needs DML there; it must never be able to drop or alter tables.
        for privilege in ("INSERT", "UPDATE", "DELETE", "INDEX"):
            self.assertIn(privilege, BENCHMARK_GRANTS)
        for privilege in ("DROP", "ALTER", "CREATE"):
            self.assertNotIn(privilege, BENCHMARK_GRANTS)

    def test_missing_password_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Password for 'queryscale_jobs' is empty"):
            build_users(env(DB_JOBS_PASSWORD=""), LAYOUT)

    def test_admin_user_cannot_be_an_application_user(self):
        users = build_users(env(), LAYOUT)
        admin_username_is_not_an_app_user("queryscale_admin", users)
        with self.assertRaisesRegex(ValueError, "administrative user"):
            admin_username_is_not_an_app_user("queryscale_app", users)

    def test_user_statements_bind_credentials_and_scope_grants_to_one_database(self):
        user = DatabaseUser("queryscale_benchmark", "p@ss", "queryscale_test", BENCHMARK_GRANTS)
        statements = user_statements(user)
        sql = [s for s, _ in statements]
        self.assertTrue(sql[0].startswith("CREATE USER IF NOT EXISTS"))
        self.assertIn("REQUIRE SSL", sql[0])
        self.assertIn("REQUIRE SSL", sql[1])
        self.assertTrue(sql[2].startswith("REVOKE ALL PRIVILEGES, GRANT OPTION"))
        # mysql-connector needs %% for a literal % whenever parameters are bound.
        self.assertEqual(sql[3], "GRANT SELECT, INSERT, UPDATE, DELETE, INDEX ON `queryscale_test`.* TO %s@'%%'")
        for statement, params in statements:
            self.assertNotIn("p@ss", statement)
            self.assertEqual(params[0], "queryscale_benchmark")
        self.assertEqual(statements[0][1], ("queryscale_benchmark", "p@ss"))

    def test_unsafe_user_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid MySQL user name"):
            DatabaseUser("bad'user", "x", "queryscale", APP_GRANTS).validate()


class FakeCursor:
    """Minimal cursor recording executed SQL and answering fetches from a queue."""

    def __init__(self, fetch_results=None):
        self.executed: list[tuple[str, tuple | None]] = []
        self._results = list(fetch_results or [])

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._results.pop(0) if self._results else None

    def fetchall(self):
        return self._results.pop(0) if self._results else []


class OperationTests(unittest.TestCase):
    def test_ensure_databases_never_drops(self):
        cursor = FakeCursor()
        aws_bootstrap.ensure_databases(cursor, LAYOUT)
        sql = [s for s, _ in cursor.executed]
        self.assertEqual(len(sql), 3)
        self.assertTrue(all(s.startswith("CREATE DATABASE IF NOT EXISTS") for s in sql))
        self.assertFalse(any("DROP" in s.upper() for s in sql))

    def test_apply_schema_skip_executes_nothing(self):
        cursor = FakeCursor()
        statements = SchemaStatements(("CREATE TABLE `A` (id INT)",), ("DROP TABLE IF EXISTS `A`",))
        aws_bootstrap.apply_schema(cursor, LAYOUT, statements, "skip")
        self.assertEqual(cursor.executed, [])

    def test_apply_schema_create_does_not_drop(self):
        cursor = FakeCursor()
        statements = SchemaStatements(("CREATE TABLE `A` (id INT)",), ("DROP TABLE IF EXISTS `A`",))
        aws_bootstrap.apply_schema(cursor, LAYOUT, statements, "create")
        sql = [s for s, _ in cursor.executed]
        self.assertEqual(sql, ["USE `queryscale`", "CREATE TABLE `A` (id INT)"])

    def test_apply_schema_recreate_drops_then_creates(self):
        cursor = FakeCursor()
        statements = SchemaStatements(("CREATE TABLE `A` (id INT)",), ("DROP TABLE IF EXISTS `A`",))
        aws_bootstrap.apply_schema(cursor, LAYOUT, statements, "recreate")
        sql = [s for s, _ in cursor.executed]
        self.assertEqual(sql, ["USE `queryscale`", "DROP TABLE IF EXISTS `A`", "CREATE TABLE `A` (id INT)"])

    def test_assert_tls_fails_closed_when_required(self):
        with self.assertRaisesRegex(RuntimeError, "NOT encrypted"):
            aws_bootstrap.assert_tls(FakeCursor([("Ssl_cipher", "")]), require=True)
        aws_bootstrap.assert_tls(FakeCursor([("Ssl_cipher", "")]), require=False)
        aws_bootstrap.assert_tls(FakeCursor([("Ssl_cipher", "TLS_AES_256_GCM_SHA384")]), require=True)

    def test_cli_defaults_are_non_destructive(self):
        args = aws_bootstrap.parse_args([])
        self.assertFalse(args.reinitialize_schema)
        self.assertFalse(args.reseed)
        self.assertFalse(args.rebuild_test_db)
        self.assertEqual(args.customers, aws_bootstrap.DEFAULT_CONFIG["customers"])


class ValidationHelperTests(unittest.TestCase):
    def test_row_counts_must_be_positive_and_equal(self):
        source = {t: 10 for t in EXPECTED_TABLES}
        checks = compare_row_counts(source, dict(source))
        self.assertTrue(all(c.ok for c in checks))
        checks = compare_row_counts(source, {**source, "Loans": 9})
        self.assertFalse(next(c for c in checks if c.name == "rows:Loans").ok)
        checks = compare_row_counts({**source, "Branches": 0}, source)
        self.assertFalse(next(c for c in checks if c.name == "rows:Branches").ok)
        self.assertFalse(next(c for c in compare_row_counts({}, {}) if c.name == "rows:Customers").ok)

    def test_leftover_candidate_index_is_detected(self):
        source = {"Transactions": {"PRIMARY", "idx_transactions_account_id"}}
        benchmark = {"Transactions": {"PRIMARY", "idx_transactions_account_id", "idx_transactions_txn_type"}}
        self.assertEqual(leftover_indexes(source, benchmark), {"Transactions": {"idx_transactions_txn_type"}})
        self.assertEqual(leftover_indexes(source, source), {})

    def test_show_grants_lines_are_normalized_and_compared(self):
        user = DatabaseUser("queryscale_app", "", "queryscale", APP_GRANTS)
        self.assertEqual(normalize_grant("GRANT USAGE ON *.* TO `queryscale_app`@`%`"), (frozenset({"USAGE"}), "*.*"))
        good = ["GRANT USAGE ON *.* TO `queryscale_app`@`%`", "GRANT SELECT ON `queryscale`.* TO `queryscale_app`@`%`"]
        self.assertTrue(grants_match(good, user).ok)
        too_much = good + ["GRANT INSERT ON `queryscale_jobs`.* TO `queryscale_app`@`%`"]
        self.assertFalse(grants_match(too_much, user).ok)
        global_privs = ["GRANT SELECT ON *.* TO `queryscale_app`@`%`", good[1]]
        self.assertFalse(grants_match(global_privs, user).ok)
        self.assertFalse(grants_match(["GRANT USAGE ON *.* TO `queryscale_app`@`%`"], user).ok)

    def test_forbidden_queries_target_other_databases_and_writes(self):
        app = DatabaseUser("queryscale_app", "", "queryscale", APP_GRANTS)
        descriptions = [d for d, _ in forbidden_queries(app, LAYOUT)]
        self.assertIn("read queryscale_test", descriptions)
        self.assertIn("read queryscale_jobs", descriptions)
        self.assertIn("write queryscale", descriptions)
        jobs = DatabaseUser("queryscale_jobs", "", "queryscale_jobs", JOBS_GRANTS)
        descriptions = [d for d, _ in forbidden_queries(jobs, LAYOUT)]
        self.assertNotIn("write queryscale_jobs", descriptions)
        self.assertIn("drop in queryscale_jobs", descriptions)

    def test_probe_classification(self):
        def denied():
            raise MySQLError(msg="denied", errno=1142)

        def other():
            raise MySQLError(msg="boom", errno=1064)

        self.assertTrue(classify_probe(denied)[0])
        self.assertFalse(classify_probe(other)[0])
        self.assertFalse(classify_probe(lambda: None)[0])

    def test_check_is_serialisable(self):
        self.assertEqual(Check("x", True, "y").__dict__, {"name": "x", "ok": True, "detail": "y"})
        self.assertTrue(callable(aws_db_validate.main))


if __name__ == "__main__":
    unittest.main()
