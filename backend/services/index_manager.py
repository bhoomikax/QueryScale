"""Validate, apply, inspect, and remove candidate indexes on the isolated test database.

Every connection in this module targets ``DB_TEST_NAME``. A configuration in
which the test database name equals the primary database name is rejected, so
the primary database is never modified by benchmarking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import mysql.connector
from mysql.connector import MySQLConnection
from mysql.connector.errors import Error as MySQLError

from backend.config import settings
from backend.services.test_database import EXPECTED_TABLES, assert_distinct_databases, quote_identifier


_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# MySQL error numbers grouped into the failure categories reported by the API.
_ERROR_CODES = {
    1049: "test_database_missing",
    1044: "permission_denied",
    1045: "authentication_failed",
    1142: "permission_denied",
    1143: "permission_denied",
    1227: "permission_denied",
    3024: "query_timeout",
    1317: "query_timeout",
    2003: "connection_error",
    2005: "connection_error",
    2006: "connection_error",
    2013: "connection_error",
    1061: "index_already_exists",
    1072: "invalid_column",
    1146: "table_missing",
}


class BenchmarkError(RuntimeError):
    """A classified failure from test-database operations."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def classify_mysql_error(exc: BaseException) -> str:
    errno = getattr(exc, "errno", None)
    return _ERROR_CODES.get(errno, "database_error")


def raise_classified(exc: BaseException, action: str) -> None:
    code = classify_mysql_error(exc)
    hints = {
        "test_database_missing": f"Test database '{settings.db_test_name}' does not exist. Run: py -m backend.services.test_database",
        "permission_denied": "The configured MySQL user lacks the privilege required for this step.",
        "authentication_failed": "MySQL rejected the configured credentials. Set DB_USER and DB_PASSWORD in .env.",
        "query_timeout": f"A query exceeded BENCHMARK_QUERY_TIMEOUT_MS={settings.benchmark_query_timeout_ms}.",
        "connection_error": "Lost or could not open the MySQL connection.",
    }
    raise BenchmarkError(code, f"{action} failed ({code}): {hints.get(code, exc)}") from exc


@dataclass(frozen=True)
class IndexApplication:
    """Details of an index created in the test database."""

    candidate_id: str
    index_name: str
    table: str
    columns: tuple[str, ...]
    sql: str


def normalize_columns(columns: str | Sequence[str]) -> tuple[str, ...]:
    """Normalize a recommendation's column value and reject unsafe names."""

    values = columns.split(",") if isinstance(columns, str) else columns
    normalized = tuple(column.strip() for column in values if column.strip())
    if not normalized:
        raise ValueError("An index candidate must include at least one column")
    if len(set(column.lower() for column in normalized)) != len(normalized):
        raise ValueError("An index candidate cannot contain duplicate columns")
    if any(not _IDENTIFIER_PATTERN.fullmatch(column) for column in normalized):
        raise ValueError("Index columns must be valid MySQL identifiers")
    return normalized


def validate_candidate(candidate_id: str, table: str, columns: str | Sequence[str]) -> tuple[str, ...]:
    """Validate a recommendation's identity and schema-level identifiers."""

    if table not in EXPECTED_TABLES:
        raise ValueError(f"Unsupported QueryScale table: {table!r}")
    if not _IDENTIFIER_PATTERN.fullmatch(table):
        raise ValueError(f"Invalid table identifier: {table!r}")

    normalized_columns = normalize_columns(columns)
    expected_id = f"IDX_{table.upper()}_{'_'.join(column.upper() for column in normalized_columns)}"
    if candidate_id != expected_id:
        raise ValueError(
            f"Candidate ID does not match table and columns: expected {expected_id!r}"
        )
    return normalized_columns


def build_index_name(table: str, columns: Sequence[str]) -> str:
    """Build the deterministic index name used by QueryScale."""

    index_name = f"idx_{table.lower()}_{'_'.join(column.lower() for column in columns)}"
    if len(index_name) > 64:
        raise ValueError("Generated MySQL index name exceeds the 64-character limit")
    return index_name


def build_create_index_sql(index_name: str, table: str, columns: Sequence[str]) -> str:
    """Build SQL from already validated identifiers."""

    quoted_columns = ", ".join(quote_identifier(column) for column in columns)
    return f"CREATE INDEX {quote_identifier(index_name)} ON {quote_identifier(table)} ({quoted_columns})"


def build_drop_index_sql(index_name: str, table: str) -> str:
    return f"DROP INDEX {quote_identifier(index_name)} ON {quote_identifier(table)}"


def _test_database_config() -> dict[str, object]:
    assert_distinct_databases()
    # Benchmark user and test database only (DB_TEST_USER falls back to DB_USER locally).
    return settings.test_mysql_config()


def connect_to_test_database(raise_on_warnings: bool = True) -> MySQLConnection:
    config = _test_database_config()
    config["raise_on_warnings"] = raise_on_warnings
    try:
        return mysql.connector.connect(**config)
    except MySQLError as exc:
        raise_classified(exc, f"Connecting to test database '{settings.db_test_name}'")
        raise  # unreachable; keeps type checkers satisfied


# Kept for backward compatibility with earlier imports.
_connect_to_test_database = connect_to_test_database


def _validate_columns_exist(cursor, table: str, columns: Sequence[str]) -> None:
    cursor.execute(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
        (settings.db_test_name, table),
    )
    available_columns = {row[0].lower() for row in cursor.fetchall()}
    missing_columns = [column for column in columns if column.lower() not in available_columns]
    if missing_columns:
        raise ValueError(f"Columns do not exist on {table}: {', '.join(missing_columns)}")


def list_test_indexes(cursor) -> Dict[str, Dict[str, List[str]]]:
    """Return ``{table_lower: {index_name: [columns]}}`` for the test database."""

    cursor.execute(
        "SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.STATISTICS "
        "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX",
        (settings.db_test_name,),
    )
    indexes: Dict[str, Dict[str, List[str]]] = {}
    for table, index, column in cursor.fetchall():
        indexes.setdefault(str(table).lower(), {}).setdefault(index, []).append(column)
    return indexes


def apply_index(candidate_id: str, table: str, columns: str | Sequence[str]) -> IndexApplication:
    """Validate and create one candidate index in the configured test database."""

    normalized_columns = validate_candidate(candidate_id, table, columns)
    index_name = build_index_name(table, normalized_columns)
    sql = build_create_index_sql(index_name, table, normalized_columns)
    connection = connect_to_test_database()
    cursor = connection.cursor()
    try:
        _validate_columns_exist(cursor, table, normalized_columns)
        cursor.execute(sql)
        connection.commit()
        return IndexApplication(candidate_id, index_name, table, normalized_columns, sql)
    except MySQLError as exc:
        connection.rollback()
        raise_classified(exc, f"Creating index for candidate {candidate_id!r}")
        raise
    finally:
        cursor.close()
        connection.close()


def drop_index(index_name: str, table: str) -> bool:
    """Drop an index from the test database. Returns False if it did not exist."""

    if table not in EXPECTED_TABLES:
        raise ValueError(f"Unsupported QueryScale table: {table!r}")
    connection = connect_to_test_database(raise_on_warnings=False)
    cursor = connection.cursor()
    try:
        existing = list_test_indexes(cursor).get(table.lower(), {})
        if index_name not in existing:
            return False
        cursor.execute(build_drop_index_sql(index_name, table))
        return True
    except MySQLError as exc:
        raise_classified(exc, f"Dropping index {index_name!r}")
        raise
    finally:
        cursor.close()
        connection.close()


def reset_test_indexes(reference: Dict[str, set[str]]) -> List[str]:
    """Drop test-database indexes that are not in ``reference`` (``{table_lower: {index names}}``).

    This removes indexes left behind by earlier or interrupted benchmarks so
    each run starts from the same index configuration as the primary schema.
    """

    connection = connect_to_test_database(raise_on_warnings=False)
    cursor = connection.cursor()
    dropped: List[str] = []
    try:
        for table_lower, indexes in list_test_indexes(cursor).items():
            table = next((t for t in EXPECTED_TABLES if t.lower() == table_lower), None)
            if table is None:
                continue
            allowed = {name.lower() for name in reference.get(table_lower, set())}
            for index_name in indexes:
                if index_name == "PRIMARY" or index_name.lower() in allowed:
                    continue
                cursor.execute(build_drop_index_sql(index_name, table))
                dropped.append(f"{table}.{index_name}")
        return dropped
    except MySQLError as exc:
        raise_classified(exc, "Resetting test database indexes")
        raise
    finally:
        cursor.close()
        connection.close()


def measure_index_size_bytes(cursor, table: str, index_name: str) -> Optional[int]:
    """Measured index size from InnoDB persistent statistics, or None if unavailable.

    Runs ANALYZE TABLE on the test database first so statistics are current.
    """

    try:
        cursor.execute(f"ANALYZE TABLE {quote_identifier(table)}")
        cursor.fetchall()
        cursor.execute(
            "SELECT s.stat_value * @@innodb_page_size FROM mysql.innodb_index_stats s "
            "WHERE s.database_name = %s AND LOWER(s.table_name) = LOWER(%s) AND s.index_name = %s "
            "AND s.stat_name = 'size'",
            (settings.db_test_name, table, index_name),
        )
        row = cursor.fetchone()
    except MySQLError:
        return None
    return int(row[0]) if row and row[0] is not None else None


if __name__ == "__main__":
    print(build_create_index_sql("idx_transactions_account_id", "Transactions", ("account_id",)))


__all__ = [
    "BenchmarkError",
    "IndexApplication",
    "apply_index",
    "build_create_index_sql",
    "build_drop_index_sql",
    "build_index_name",
    "classify_mysql_error",
    "connect_to_test_database",
    "drop_index",
    "list_test_indexes",
    "measure_index_size_bytes",
    "normalize_columns",
    "reset_test_indexes",
    "validate_candidate",
]
