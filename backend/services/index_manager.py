"""Validate and apply recommended indexes to the isolated test database."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

import mysql.connector
from mysql.connector import MySQLConnection
from mysql.connector.errors import Error as MySQLError

from backend.config import settings
from backend.database import close_connection, open_connection
from backend.services.test_database import EXPECTED_TABLES, quote_identifier


_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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


def _test_database_config() -> dict[str, object]:
    if settings.db_name == settings.db_test_name:
        raise ValueError("DB_TEST_NAME must differ from DB_NAME")
    return settings.test_mysql_config()


def _connect_to_test_database() -> MySQLConnection:
    try:
        return open_connection(_test_database_config())
    except MySQLError as exc:
        raise RuntimeError(
            f"Unable to connect to test database '{settings.db_test_name}'. "
            "Run the test database setup first and check .env."
        ) from exc


def _validate_columns_exist(cursor, table: str, columns: Sequence[str]) -> None:
    cursor.execute(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
        (settings.db_test_name, table),
    )
    available_columns = {row[0] for row in cursor.fetchall()}
    missing_columns = [column for column in columns if column not in available_columns]
    if missing_columns:
        raise ValueError(f"Columns do not exist on {table}: {', '.join(missing_columns)}")


def candidate_index_exists(candidate_id: str, table: str, columns: str | Sequence[str]) -> bool:
    """Check only the deterministic candidate index in the configured test database."""

    normalized_columns = validate_candidate(candidate_id, table, columns)
    index_name = build_index_name(table, normalized_columns)
    connection = _connect_to_test_database()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND INDEX_NAME = %s LIMIT 1",
            (settings.db_test_name, table, index_name),
        )
        return cursor.fetchone() is not None
    finally:
        cursor.close()
        close_connection(connection)


def remove_candidate_index(candidate_id: str, table: str, columns: str | Sequence[str]) -> bool:
    """Remove only a validated candidate index from DB_TEST_NAME, if it exists."""

    normalized_columns = validate_candidate(candidate_id, table, columns)
    index_name = build_index_name(table, normalized_columns)
    connection = _connect_to_test_database()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND INDEX_NAME = %s LIMIT 1",
            (settings.db_test_name, table, index_name),
        )
        if cursor.fetchone() is None:
            return False
        cursor.execute(
            f"DROP INDEX {quote_identifier(index_name)} ON {quote_identifier(table)}"
        )
        connection.commit()
        return True
    except MySQLError as exc:
        connection.rollback()
        raise RuntimeError(f"Unable to clean benchmark index {candidate_id!r} from the test database.") from exc
    finally:
        cursor.close()
        close_connection(connection)


def apply_index(candidate_id: str, table: str, columns: str | Sequence[str]) -> IndexApplication:
    """Validate and create one candidate index in the configured test database."""

    normalized_columns = validate_candidate(candidate_id, table, columns)
    index_name = build_index_name(table, normalized_columns)
    sql = build_create_index_sql(index_name, table, normalized_columns)
    connection = _connect_to_test_database()
    cursor = connection.cursor()
    try:
        _validate_columns_exist(cursor, table, normalized_columns)
        cursor.execute(sql)
        connection.commit()
        return IndexApplication(candidate_id, index_name, table, normalized_columns, sql)
    except MySQLError as exc:
        connection.rollback()
        raise RuntimeError(f"Unable to apply index candidate {candidate_id!r}.") from exc
    finally:
        cursor.close()
        close_connection(connection)


if __name__ == "__main__":
    print(build_create_index_sql("idx_transactions_account_id", "Transactions", ("account_id",)))
