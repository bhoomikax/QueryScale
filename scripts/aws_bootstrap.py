#!/usr/bin/env python3
"""Idempotent database bootstrap for QueryScale on RDS (or any MySQL 8 server).

Run with ``python -m scripts.aws_bootstrap`` from the project root, normally as the
ECS bootstrap task (infra/terraform/ecs_bootstrap.tf). It is the only process that
holds the administrative credential, injected as ``DB_USER`` / ``DB_PASSWORD`` so the
repository's existing seed and test-database helpers work unchanged.

What it does, in order, and what it refuses to do:

1. Verifies the connection is TLS (unless ``BOOTSTRAP_REQUIRE_TLS=false``).
2. Creates the source, benchmark and jobs databases with ``IF NOT EXISTS``. Never drops
   the source or jobs database.
3. Creates or updates the three application users, each ``REQUIRE SSL``, and converges
   their grants to the least-privilege set below. The administrative user is never
   handed to the API or worker.
4. Loads ``database/schema.sql`` into the source database only when none of its tables
   exist. A partially initialised database aborts; ``--reinitialize-schema`` is the
   explicit, destructive override that drops and recreates the tables.
5. Seeds synthetic data with ``scripts/seed.py`` only when every table is empty;
   ``--reseed`` truncates and reloads.
6. Clones the benchmark database with ``backend.services.test_database`` only when it is
   missing or empty; ``--rebuild-test-db`` drops and re-clones it. The clone helper
   already refuses to run when ``DB_TEST_NAME`` equals ``DB_NAME``.

Grants (database-scoped, nothing global):

    DB_APP_USER        SELECT                                      on DB_NAME.*
    DB_BENCHMARK_USER  SELECT, INDEX                               on DB_TEST_NAME.*
    DB_JOBS_USER       SELECT, INSERT, UPDATE, DELETE, CREATE,
                       ALTER, INDEX                                on DB_JOBS_NAME.*

The jobs user may create tables because the job store creates its own schema on first
use (see backend/jobs/store.py). The exact DB_JOBS_* names are an interface proposal
until Person 1's MySQL job store lands.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from mysql.connector.errors import Error as MySQLError

from backend.config import settings
from backend.database import close_connection, open_connection
from backend.services.test_database import EXPECTED_TABLES, clone_test_database, quote_identifier
from scripts.seed import DEFAULT_CONFIG, generate_dataset, insert_dataset, reset_tables

LOGGER = logging.getLogger("queryscale.bootstrap")
SCHEMA_PATH = ROOT_DIR / "database" / "schema.sql"
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

APP_GRANTS: tuple[str, ...] = ("SELECT",)
BENCHMARK_GRANTS: tuple[str, ...] = ("SELECT", "INDEX")
JOBS_GRANTS: tuple[str, ...] = ("SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "INDEX")


# ---------------------------------------------------------------------------
# Pure, testable pieces
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DatabaseLayout:
    source: str
    benchmark: str
    jobs: str

    def validate(self) -> "DatabaseLayout":
        names = (self.source, self.benchmark, self.jobs)
        for name in names:
            if not _IDENTIFIER_PATTERN.fullmatch(name):
                raise ValueError(f"Invalid database identifier: {name!r}")
        if len({name.lower() for name in names}) != 3:
            raise ValueError(
                "DB_NAME, DB_TEST_NAME and DB_JOBS_NAME must be three different databases "
                f"(got {self.source!r}, {self.benchmark!r}, {self.jobs!r})"
            )
        return self


@dataclass(frozen=True)
class DatabaseUser:
    username: str
    password: str
    database: str
    grants: tuple[str, ...]

    def validate(self) -> "DatabaseUser":
        if not _IDENTIFIER_PATTERN.fullmatch(self.username) or len(self.username) > 32:
            raise ValueError(f"Invalid MySQL user name: {self.username!r}")
        if not self.password:
            raise ValueError(f"Password for {self.username!r} is empty")
        return self


@dataclass(frozen=True)
class SchemaStatements:
    """database/schema.sql split into the parts the bootstrap may or may not run."""

    creates: tuple[str, ...]
    drops: tuple[str, ...]
    skipped: tuple[str, ...] = field(default_factory=tuple)


def load_schema_statements(path: Path = SCHEMA_PATH) -> SchemaStatements:
    """Split schema.sql like scripts/run_local_pipeline.py does, then classify.

    The file hardcodes ``CREATE DATABASE queryscale`` and ``USE queryscale``. Those are
    skipped so DB_NAME stays configurable; the connection selects the database instead.
    """

    creates: list[str] = []
    drops: list[str] = []
    skipped: list[str] = []
    for raw in path.read_text(encoding="utf-8").split(";"):
        statement = raw.strip()
        if not statement:
            continue
        head = statement.upper()
        if head.startswith("CREATE TABLE"):
            creates.append(statement)
        elif head.startswith("DROP TABLE"):
            drops.append(statement)
        elif head.startswith("CREATE DATABASE") or head.startswith("USE "):
            skipped.append(statement)
        else:
            raise ValueError(f"Unexpected statement in {path.name}: {statement[:60]!r}")
    if len(creates) != len(EXPECTED_TABLES):
        raise ValueError(
            f"{path.name} defines {len(creates)} tables; expected {len(EXPECTED_TABLES)}"
        )
    return SchemaStatements(tuple(creates), tuple(drops), tuple(skipped))


def decide_schema_action(present_tables: Iterable[str], reinitialize: bool) -> str:
    present = {table for table in present_tables if table in EXPECTED_TABLES}
    if reinitialize:
        return "recreate"
    if not present:
        return "create"
    if present == set(EXPECTED_TABLES):
        return "skip"
    missing = sorted(set(EXPECTED_TABLES) - present)
    raise RuntimeError(
        f"Source database is partially initialised (missing {', '.join(missing)}). "
        "Refusing to guess; rerun with --reinitialize-schema to drop and recreate the tables."
    )


def decide_seed_action(row_counts: dict[str, int], reseed: bool) -> str:
    if reseed:
        return "reseed"
    if any(row_counts.get(table, 0) > 0 for table in EXPECTED_TABLES):
        return "skip"
    return "seed"


def decide_test_db_action(test_tables: Iterable[str], rebuild: bool) -> str:
    if rebuild:
        return "rebuild"
    present = {table for table in test_tables if table in EXPECTED_TABLES}
    return "skip" if present == set(EXPECTED_TABLES) else "rebuild"


def user_statements(user: DatabaseUser) -> list[tuple[str, tuple[str, ...]]]:
    """SQL to create/update one user and converge its grants. Parameters are bound."""

    user.validate()
    target = quote_identifier(user.database)
    privileges = ", ".join(user.grants)
    return [
        ("CREATE USER IF NOT EXISTS %s@'%%' IDENTIFIED BY %s REQUIRE SSL", (user.username, user.password)),
        ("ALTER USER %s@'%%' IDENTIFIED BY %s REQUIRE SSL", (user.username, user.password)),
        ("REVOKE ALL PRIVILEGES, GRANT OPTION FROM %s@'%%'", (user.username,)),
        (f"GRANT {privileges} ON {target}.* TO %s@'%%'", (user.username,)),
    ]


def build_users(env: dict[str, str], layout: DatabaseLayout) -> list[DatabaseUser]:
    """Read the three application users from the environment the task definition sets."""

    return [
        DatabaseUser(env.get("DB_APP_USER", "queryscale_app"), env.get("DB_APP_PASSWORD", ""), layout.source, APP_GRANTS).validate(),
        DatabaseUser(env.get("DB_BENCHMARK_USER", "queryscale_benchmark"), env.get("DB_BENCHMARK_PASSWORD", ""), layout.benchmark, BENCHMARK_GRANTS).validate(),
        DatabaseUser(env.get("DB_JOBS_USER", "queryscale_jobs"), env.get("DB_JOBS_PASSWORD", ""), layout.jobs, JOBS_GRANTS).validate(),
    ]


def layout_from_settings(env: dict[str, str] | None = None) -> DatabaseLayout:
    env = os.environ if env is None else env
    return DatabaseLayout(
        source=settings.db_name,
        benchmark=settings.db_test_name,
        jobs=env.get("DB_JOBS_NAME", "queryscale_jobs"),
    ).validate()


def admin_username_is_not_an_app_user(admin: str, users: Sequence[DatabaseUser]) -> None:
    if any(admin.lower() == user.username.lower() for user in users):
        raise ValueError(
            f"DB_USER ({admin!r}) must be the administrative user, not one of the application users"
        )


# ---------------------------------------------------------------------------
# Database operations (each takes a cursor so the logic above stays testable)
# ---------------------------------------------------------------------------


def _server_config() -> dict[str, object]:
    config = settings.mysql_config()
    config.pop("database", None)
    config["raise_on_warnings"] = False  # IF NOT EXISTS emits notes, not errors.
    return config


def connect_admin():
    try:
        return open_connection(_server_config())
    except MySQLError as exc:
        raise RuntimeError(
            f"Unable to connect to MySQL at {settings.db_host}:{settings.db_port} as {settings.db_user!r}."
        ) from exc


def assert_tls(cursor, require: bool) -> None:
    cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
    row = cursor.fetchone()
    cipher = row[1] if row else ""
    if cipher:
        LOGGER.info("TLS connection established (%s)", cipher)
        return
    message = "Connection to MySQL is NOT encrypted."
    if require:
        raise RuntimeError(message + " Set DB_TLS_CA to the RDS bundle or BOOTSTRAP_REQUIRE_TLS=false for local MySQL.")
    LOGGER.warning(message + " Continuing because BOOTSTRAP_REQUIRE_TLS=false.")


def fetch_tables(cursor, database: str) -> set[str]:
    cursor.execute(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = %s",
        (database,),
    )
    return {row[0] for row in cursor.fetchall()}


def fetch_row_counts(cursor, database: str, tables: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in tables:
        cursor.execute(f"SELECT COUNT(*) FROM {quote_identifier(database)}.{quote_identifier(table)}")
        counts[table] = int(cursor.fetchone()[0])
    return counts


def ensure_databases(cursor, layout: DatabaseLayout) -> None:
    for name in (layout.source, layout.benchmark, layout.jobs):
        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS {quote_identifier(name)} "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        LOGGER.info("Database present: %s", name)


def ensure_users(cursor, users: Sequence[DatabaseUser]) -> None:
    for user in users:
        for sql, params in user_statements(user):
            cursor.execute(sql, params)
        LOGGER.info("User converged: %s -> %s on %s", user.username, ", ".join(user.grants), user.database)
    cursor.execute("FLUSH PRIVILEGES")


def apply_schema(cursor, layout: DatabaseLayout, statements: SchemaStatements, action: str) -> None:
    if action == "skip":
        LOGGER.info("Schema already present in %s; skipping", layout.source)
        return
    cursor.execute(f"USE {quote_identifier(layout.source)}")
    if action == "recreate":
        LOGGER.warning("Dropping and recreating tables in %s (--reinitialize-schema)", layout.source)
        for statement in statements.drops:
            cursor.execute(statement)
    for statement in statements.creates:
        cursor.execute(statement)
    LOGGER.info("Schema loaded into %s (%d tables)", layout.source, len(statements.creates))


def seed_source(connection, layout: DatabaseLayout, sizes: dict[str, int], action: str) -> None:
    if action == "skip":
        LOGGER.info("Source database already contains data; skipping seed (use --reseed to reload)")
        return
    cursor = connection.cursor()
    try:
        cursor.execute(f"USE {quote_identifier(layout.source)}")
        if action == "reseed":
            LOGGER.warning("Truncating and reloading synthetic data (--reseed)")
            reset_tables(cursor)
        insert_dataset(cursor, generate_dataset(sizes))
        connection.commit()
    finally:
        cursor.close()
    LOGGER.info("Seeded %s: %s", layout.source, ", ".join(f"{k}={v}" for k, v in sizes.items()))


def rebuild_test_database(action: str) -> None:
    if action == "skip":
        LOGGER.info("Benchmark database already cloned; skipping (use --rebuild-test-db to refresh)")
        return
    tables = clone_test_database()  # Drops and recreates DB_TEST_NAME only; refuses DB_TEST_NAME == DB_NAME.
    LOGGER.info("Cloned %s -> %s (%s)", settings.db_name, settings.db_test_name, ", ".join(tables))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap QueryScale databases, users and data idempotently.")
    parser.add_argument("--reinitialize-schema", action="store_true", help="DESTRUCTIVE: drop and recreate the source tables.")
    parser.add_argument("--reseed", action="store_true", help="DESTRUCTIVE: truncate and reload synthetic data.")
    parser.add_argument("--rebuild-test-db", action="store_true", help="Drop and re-clone the disposable benchmark database.")
    parser.add_argument("--skip-users", action="store_true", help="Do not create or modify application users.")
    parser.add_argument("--skip-seed", action="store_true", help="Do not load synthetic data even if tables are empty.")
    parser.add_argument("--skip-test-db", action="store_true", help="Do not clone the benchmark database.")
    for key, value in DEFAULT_CONFIG.items():
        parser.add_argument(f"--{key}", type=int, default=value, help=f"Synthetic {key} rows (default {value}).")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    require_tls = os.environ.get("BOOTSTRAP_REQUIRE_TLS", "true").strip().lower() not in {"0", "false", "no", "off"}

    layout = layout_from_settings()
    users = [] if args.skip_users else build_users(dict(os.environ), layout)
    admin_username_is_not_an_app_user(settings.db_user, users)
    statements = load_schema_statements()
    sizes = {key: getattr(args, key) for key in DEFAULT_CONFIG}

    LOGGER.info("Bootstrapping %s:%s as %s (source=%s benchmark=%s jobs=%s)",
                settings.db_host, settings.db_port, settings.db_user, layout.source, layout.benchmark, layout.jobs)

    connection = connect_admin()
    try:
        cursor = connection.cursor()
        try:
            assert_tls(cursor, require_tls)
            ensure_databases(cursor, layout)
            if users:
                ensure_users(cursor, users)

            schema_action = decide_schema_action(fetch_tables(cursor, layout.source), args.reinitialize_schema)
            apply_schema(cursor, layout, statements, schema_action)
            connection.commit()

            if not args.skip_seed:
                counts = fetch_row_counts(cursor, layout.source, EXPECTED_TABLES)
                seed_action = decide_seed_action(counts, args.reseed)
            else:
                seed_action = "skip"
        finally:
            cursor.close()

        seed_source(connection, layout, sizes, seed_action)
    finally:
        close_connection(connection)

    if not args.skip_test_db:
        connection = connect_admin()
        try:
            cursor = connection.cursor()
            try:
                test_action = decide_test_db_action(fetch_tables(cursor, layout.benchmark), args.rebuild_test_db)
            finally:
                cursor.close()
        finally:
            close_connection(connection)
        rebuild_test_database(test_action)

    LOGGER.info("Bootstrap complete. Run `python -m scripts.aws_db_validate` to verify.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
