#!/usr/bin/env python3
"""Validate the QueryScale database layout after bootstrap.

Run with ``python -m scripts.aws_db_validate`` using the same environment as
``scripts.aws_bootstrap`` (administrative ``DB_USER``). Exit code 0 means every check
passed. Checks:

* the three databases exist and are distinct;
* the five expected tables exist in the source and benchmark databases;
* every table has rows and the benchmark copy matches the source row for row;
* the benchmark database carries no index the source lacks (a leftover candidate
  index would make the worker fail with ``candidate_index_exists``);
* each application user exists, requires SSL, and holds exactly the expected grants;
* the server enforces ``require_secure_transport`` (unless ``--no-require-tls``);
* when the application passwords are present in the environment, each user is
  connected as and proven unable to reach the databases it must not touch.

Use ``--json`` for machine-readable output.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from mysql.connector.errors import Error as MySQLError

from backend.config import settings
from backend.database import close_connection, open_connection
from backend.services.test_database import EXPECTED_TABLES, quote_identifier
from scripts.aws_bootstrap import DatabaseLayout, DatabaseUser, build_users, layout_from_settings

LOGGER = logging.getLogger("queryscale.validate")
_ACCESS_DENIED_ERRNOS = {1044, 1142, 1143, 1227, 1370}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def compare_row_counts(source: dict[str, int], benchmark: dict[str, int]) -> list[Check]:
    checks: list[Check] = []
    for table in EXPECTED_TABLES:
        src, bench = source.get(table), benchmark.get(table)
        if src is None or bench is None:
            checks.append(Check(f"rows:{table}", False, "table missing"))
        elif src <= 0:
            checks.append(Check(f"rows:{table}", False, "source table is empty"))
        elif src != bench:
            checks.append(Check(f"rows:{table}", False, f"source={src} benchmark={bench}"))
        else:
            checks.append(Check(f"rows:{table}", True, f"{src} rows in both"))
    return checks


def leftover_indexes(source: dict[str, set[str]], benchmark: dict[str, set[str]]) -> dict[str, set[str]]:
    """Indexes present in the benchmark copy but absent from the source, per table."""

    return {
        table: benchmark.get(table, set()) - source.get(table, set())
        for table in EXPECTED_TABLES
        if benchmark.get(table, set()) - source.get(table, set())
    }


_GRANT_PATTERN = re.compile(r"^GRANT (?P<privs>.+?) ON (?P<scope>\S+) TO ", re.IGNORECASE)


def normalize_grant(grant_line: str) -> tuple[frozenset[str], str] | None:
    """Turn one SHOW GRANTS line into (privileges, scope) for comparison."""

    match = _GRANT_PATTERN.match(grant_line.strip())
    if not match:
        return None
    privileges = frozenset(p.strip().upper() for p in match.group("privs").split(","))
    scope = match.group("scope").replace("`", "").replace("'", "").replace('"', "").lower()
    return privileges, scope


def expected_grant(user: DatabaseUser) -> tuple[frozenset[str], str]:
    return frozenset(g.upper() for g in user.grants), f"{user.database.lower()}.*"


def grants_match(show_grants_lines: Iterable[str], user: DatabaseUser) -> Check:
    """Exactly one database-scoped grant line with the expected privileges, and only USAGE globally."""

    parsed = [normalize_grant(line) for line in show_grants_lines]
    scoped = [item for item in parsed if item and item[1] != "*.*"]
    global_privs = [item[0] for item in parsed if item and item[1] == "*.*"]
    extra_global = [privs for privs in global_privs if privs != frozenset({"USAGE"})]
    expected = expected_grant(user)
    if extra_global:
        return Check(f"grants:{user.username}", False, f"unexpected global privileges {sorted(extra_global[0])}")
    if scoped == [expected]:
        return Check(f"grants:{user.username}", True, f"{', '.join(sorted(expected[0]))} on {expected[1]}")
    return Check(f"grants:{user.username}", False, f"expected {sorted(expected[0])} on {expected[1]}, found {scoped}")


def forbidden_queries(user: DatabaseUser, layout: DatabaseLayout) -> list[tuple[str, str]]:
    """Statements the user must NOT be able to run (description, sql)."""

    other = [db for db in (layout.source, layout.benchmark, layout.jobs) if db != user.database]
    queries = [(f"read {db}", f"SELECT COUNT(*) FROM {quote_identifier(db)}.{quote_identifier(EXPECTED_TABLES[0])}") for db in other]
    if "INSERT" not in user.grants:
        queries.append((f"write {user.database}", f"INSERT INTO {quote_identifier(user.database)}.`Branches` (`branch_name`, `ifsc_code`, `city`) VALUES ('x', 'x', 'x')"))
    if "DROP" not in user.grants:
        queries.append((f"drop in {user.database}", f"DROP TABLE IF EXISTS {quote_identifier(user.database)}.`queryscale_validate_probe`"))
    return queries


def classify_probe(run: Callable[[], None]) -> tuple[bool, str]:
    """True when the statement was denied by privileges; False when it ran or failed otherwise."""

    try:
        run()
    except MySQLError as exc:
        if getattr(exc, "errno", None) in _ACCESS_DENIED_ERRNOS:
            return True, f"denied ({exc.errno})"
        return False, f"failed for another reason: {exc}"
    return False, "statement was allowed"


# ---------------------------------------------------------------------------
# Live checks
# ---------------------------------------------------------------------------


def _server_config() -> dict[str, object]:
    config = settings.mysql_config()
    config.pop("database", None)
    config["raise_on_warnings"] = False
    return config


def fetch_tables(cursor, database: str) -> set[str]:
    cursor.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = %s", (database,))
    return {row[0] for row in cursor.fetchall()}


def fetch_row_counts(cursor, database: str, tables: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in tables:
        try:
            cursor.execute(f"SELECT COUNT(*) FROM {quote_identifier(database)}.{quote_identifier(table)}")
            counts[table] = int(cursor.fetchone()[0])
        except MySQLError:
            continue
    return counts


def fetch_indexes(cursor, database: str) -> dict[str, set[str]]:
    cursor.execute(
        "SELECT TABLE_NAME, INDEX_NAME FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = %s",
        (database,),
    )
    indexes: dict[str, set[str]] = {}
    for table, index in cursor.fetchall():
        indexes.setdefault(table, set()).add(index)
    return indexes


def run_checks(layout: DatabaseLayout, users: Sequence[DatabaseUser], require_tls: bool, probe: bool) -> list[Check]:
    checks: list[Check] = []
    connection = open_connection(_server_config())
    try:
        cursor = connection.cursor()
        try:
            cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
            row = cursor.fetchone()
            cipher = row[1] if row else ""
            checks.append(Check("tls:admin-connection", bool(cipher) or not require_tls, cipher or "not encrypted"))

            cursor.execute("SHOW GLOBAL VARIABLES LIKE 'require_secure_transport'")
            row = cursor.fetchone()
            enforced = bool(row) and str(row[1]).upper() == "ON"
            checks.append(Check("tls:require_secure_transport", enforced or not require_tls, str(row[1]) if row else "unset"))

            cursor.execute("SHOW DATABASES")
            databases = {r[0] for r in cursor.fetchall()}
            for label, name in (("source", layout.source), ("benchmark", layout.benchmark), ("jobs", layout.jobs)):
                checks.append(Check(f"database:{label}", name in databases, name))

            source_tables = fetch_tables(cursor, layout.source)
            bench_tables = fetch_tables(cursor, layout.benchmark)
            checks.append(Check("tables:source", set(EXPECTED_TABLES) <= source_tables, ", ".join(sorted(set(EXPECTED_TABLES) - source_tables)) or "all present"))
            checks.append(Check("tables:benchmark", set(EXPECTED_TABLES) <= bench_tables, ", ".join(sorted(set(EXPECTED_TABLES) - bench_tables)) or "all present"))

            checks.extend(compare_row_counts(
                fetch_row_counts(cursor, layout.source, EXPECTED_TABLES),
                fetch_row_counts(cursor, layout.benchmark, EXPECTED_TABLES),
            ))

            leftovers = leftover_indexes(fetch_indexes(cursor, layout.source), fetch_indexes(cursor, layout.benchmark))
            checks.append(Check("benchmark:no-leftover-indexes", not leftovers, json.dumps({k: sorted(v) for k, v in leftovers.items()}) if leftovers else "clean"))

            for user in users:
                cursor.execute("SELECT ssl_type FROM mysql.user WHERE user = %s AND host = '%%'", (user.username,))
                row = cursor.fetchone()
                checks.append(Check(f"user:{user.username}", row is not None, "exists" if row else "missing"))
                if row is None:
                    continue
                checks.append(Check(f"ssl-required:{user.username}", str(row[0]).upper() in {"ANY", "X509", "SPECIFIED"}, str(row[0]) or "no SSL requirement"))
                cursor.execute("SHOW GRANTS FOR %s@'%%'", (user.username,))
                checks.append(grants_match([r[0] for r in cursor.fetchall()], user))
        finally:
            cursor.close()
    finally:
        close_connection(connection)

    if probe:
        for user in users:
            if not user.password:
                checks.append(Check(f"probe:{user.username}", True, "skipped, no password in environment"))
                continue
            config = settings.mysql_config()
            config.update({"user": user.username, "password": user.password, "database": user.database, "raise_on_warnings": False})
            try:
                user_connection = open_connection(config)
            except RuntimeError as exc:
                checks.append(Check(f"probe:{user.username}:connect", False, str(exc)))
                continue
            try:
                cursor = user_connection.cursor()
                try:
                    for description, sql in forbidden_queries(user, layout):
                        denied, detail = classify_probe(lambda: (cursor.execute(sql), cursor.fetchall()))
                        checks.append(Check(f"probe:{user.username}:cannot {description}", denied, detail))
                finally:
                    cursor.close()
            finally:
                close_connection(user_connection)
    return checks


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate QueryScale databases, tables, row counts and privileges.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a text report.")
    parser.add_argument("--no-require-tls", action="store_true", help="Do not fail on unencrypted connections (local MySQL).")
    parser.add_argument("--no-probe", action="store_true", help="Skip connecting as each application user.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    env_tls = os.environ.get("BOOTSTRAP_REQUIRE_TLS", "true").strip().lower() not in {"0", "false", "no", "off"}
    require_tls = env_tls and not args.no_require_tls

    layout = layout_from_settings()
    try:
        users = build_users(dict(os.environ), layout)
    except ValueError:
        # Passwords absent: still validate existence and grants, skip probes.
        users = [u for u in build_users({**os.environ, "DB_APP_PASSWORD": "-", "DB_BENCHMARK_PASSWORD": "-", "DB_JOBS_PASSWORD": "-"}, layout)]
        users = [DatabaseUser(u.username, "", u.database, u.grants) for u in users]

    checks = run_checks(layout, users, require_tls, probe=not args.no_probe)
    failed = [c for c in checks if not c.ok]
    if args.json:
        print(json.dumps({"ok": not failed, "checks": [c.__dict__ for c in checks]}, indent=2))
    else:
        for check in checks:
            print(f"[{'PASS' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
        print(f"\n{len(checks) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
