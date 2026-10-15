"""Create and populate an isolated MySQL database for index experiments."""

from __future__ import annotations

import re
from typing import Iterable

import mysql.connector
from mysql.connector import MySQLConnection
from mysql.connector.errors import Error as MySQLError

from backend.config import settings


EXPECTED_TABLES = ("Branches", "Customers", "Accounts", "Transactions", "Loans")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_identifier(identifier: str) -> str:
    """Quote a validated MySQL identifier."""

    if not _IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ValueError(f"Invalid MySQL identifier: {identifier!r}")
    return f"`{identifier}`"


def assert_distinct_databases(primary: str | None = None, test: str | None = None) -> tuple[str, str]:
    """Validate that the primary and test database names are safe and cannot refer to the same database.

    MySQL on Windows and macOS (lower_case_table_names=1 or 2) treats database
    names case-insensitively, so ``QueryScale`` and ``queryscale`` are the same
    database there. The comparison is therefore case-insensitive everywhere.
    """

    primary_name = settings.db_name if primary is None else primary
    test_name = settings.db_test_name if test is None else test
    quote_identifier(primary_name)
    quote_identifier(test_name)
    if primary_name.strip().lower() == test_name.strip().lower():
        raise ValueError(
            f"DB_TEST_NAME ({test_name!r}) must differ from DB_NAME ({primary_name!r}), "
            "compared case-insensitively. Benchmarks drop and recreate the test database."
        )
    return primary_name, test_name


def _server_config() -> dict[str, object]:
    config = settings.mysql_config()
    config.pop("database", None)
    config["raise_on_warnings"] = False
    return config


def _connect_to_server() -> MySQLConnection:
    try:
        return mysql.connector.connect(**_server_config())
    except MySQLError as exc:
        raise RuntimeError(
            f"Unable to connect to MySQL at {settings.db_host}:{settings.db_port}. "
            "Check .env and ensure MySQL is running."
        ) from exc


def _database_exists(cursor, database_name: str) -> bool:
    cursor.execute("SHOW DATABASES LIKE %s", (database_name,))
    return cursor.fetchone() is not None


def _create_database(cursor, database_name: str) -> None:
    quoted_name = quote_identifier(database_name)
    cursor.execute(
        f"CREATE DATABASE {quoted_name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )


def _copy_tables(cursor, source_database: str, test_database: str, tables: Iterable[str]) -> None:
    source = quote_identifier(source_database)
    target = quote_identifier(test_database)
    cursor.execute(f"USE {target}")
    cursor.execute("SET FOREIGN_KEY_CHECKS = 0")

    for table_name in tables:
        table = quote_identifier(table_name)
        cursor.execute(f"SHOW CREATE TABLE {source}.{table}")
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"Source table does not exist: {source_database}.{table_name}")

        cursor.execute(row[1])
        cursor.execute(f"INSERT INTO {target}.{table} SELECT * FROM {source}.{table}")

    cursor.execute("SET FOREIGN_KEY_CHECKS = 1")


def clone_test_database() -> tuple[str, ...]:
    """Replace the configured test database with a copy of the source database."""

    source_database, test_database = assert_distinct_databases()
    connection = _connect_to_server()
    cursor = connection.cursor()
    try:
        if not _database_exists(cursor, source_database):
            raise RuntimeError(f"Source database does not exist: {source_database}")

        cursor.execute(f"DROP DATABASE IF EXISTS {quote_identifier(test_database)}")
        _create_database(cursor, test_database)
        _copy_tables(cursor, source_database, test_database, EXPECTED_TABLES)
        connection.commit()
        return EXPECTED_TABLES
    except MySQLError as exc:
        connection.rollback()
        raise RuntimeError(f"Unable to create the isolated QueryScale test database: {exc}") from exc
    finally:
        cursor.close()
        connection.close()


def main() -> int:
    tables = clone_test_database()
    print(f"Cloned {settings.db_name} to {settings.db_test_name}.")
    print(f"Copied tables: {', '.join(tables)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())